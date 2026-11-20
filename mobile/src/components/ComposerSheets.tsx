import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
  type LayoutChangeEvent,
} from "react-native";
import Reanimated, { useAnimatedStyle } from "react-native-reanimated";
import {
  AtSign,
  CalendarClock,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  CircleAlert,
  Clock,
  Ellipsis,
  FileText,
  MessageCircleQuestion,
  PenLine,
  Plus,
  ShieldAlert,
  Sparkles,
  X,
} from "lucide-react-native";
import type { ComposerSheet } from "@opencuria/chat-core/composerSheets";
import type {
  HarnessPermissionRequest,
  HarnessPermissionResponse,
  HarnessQuestionRequest,
  HarnessScheduledMessage,
  HarnessSteer,
} from "@opencuria/chat-core/harness";
import { formatSendTime } from "@opencuria/chat-core/sendLater";
import type { MentionCandidate } from "@opencuria/chat-core/harnessMentions";
import {
  collectQuestionAnswers,
  emptyQuestionDraft,
  isQuestionCustomActive,
  selectedQuestionOptions,
  setQuestionCustomAnswer,
  toggleQuestionOption,
  type QuestionDraft,
} from "@opencuria/chat-core/harnessQuestion";
import { gateSourceLabel } from "@opencuria/chat-core/harnessSubtaskActivity";
import {
  ComposerSheetPeek,
  ComposerSheetSurface,
  composerSheetsFloat,
} from "../ui/ComposerSurface";
import { NativeMenu } from "../ui/NativeMenu";
import { Button, IconButton, NativeAction } from "../ui/primitives";
import { NativeOption, OPTION_ROW } from "../ui/selection";
import { useTheme } from "../ui/theme";
import { useChatLayout } from "./ChatLayout";

/** Pending agent gates; the host owns their transport and reports failures. */
export interface ComposerGates {
  questions: HarnessQuestionRequest[];
  permissions: HarnessPermissionRequest[];
  /** An answer to a pending question is on its way. */
  submitting?: boolean;
  /** A permission decision is on its way. */
  resolving?: boolean;
  onSubmitQuestion: (
    requestId: string,
    answers: string[],
  ) => void | Promise<void>;
  onSkipQuestion: (requestId: string) => void | Promise<void>;
  onResolvePermission: (
    requestId: string,
    response: HarnessPermissionResponse,
  ) => void | Promise<void>;
}

/** Actions on outgoing messages; the host owns their transport. */
export interface ComposerQueue {
  /** Messages queued for the running turn (FIFO). */
  steers: HarnessSteer[];
  /** Messages scheduled for later, earliest first. */
  scheduled: HarnessScheduledMessage[];
  /** False while the chat cannot take a message, e.g. a stopped workspace. */
  canSendNow?: boolean;
  /** Puts a queued message back into the composer. */
  onWithdrawSteer: (steerId: string) => void | Promise<void>;
  onSendNow: (messageId: string) => void | Promise<void>;
  onReschedule: (messageId: string, sendAt: Date) => void | Promise<void>;
  /** Puts a scheduled message back into the composer. */
  onEdit: (messageId: string) => void | Promise<void>;
  onDelete: (messageId: string) => void | Promise<void>;
}

export interface ComposerSheetStackProps {
  /** Ordered by `buildComposerSheets`; only the first sheet is interactive. */
  sheets: ComposerSheet[];
  gates?: ComposerGates;
  queue?: ComposerQueue;
  /** Opens a date and time picker for a scheduled message. */
  onChangeTime?: (message: HarnessScheduledMessage) => void;
  onSelectCandidate: (candidate: MentionCandidate) => void;
  testID?: string;
}

/**
 * An opaque sheet runs into the composer as in the webapp: its square lower
 * edge reaches under the composer (6pt core padding plus 8pt behind its
 * curve). A floating glass card keeps a gap instead (a negative overlap).
 */
const OVERLAP = composerSheetsFloat ? -8 : 14;
/** Visible edge of each sheet waiting beneath the top one. */
const PEEK = 7;
const MAX_PEEKS = 2;
const PEEK_HEIGHT = PEEK * (MAX_PEEKS + 1) + 22;
/**
 * Horizontal inset of a peek per level. Peeks above a translucent glass card
 * are clipped to their own edge, so they clear the card's rounded corners.
 */
function peekInset(depth: number): number {
  return composerSheetsFloat ? 10 + depth * 12 : depth * 9;
}
const NO_QUESTIONS: HarnessQuestionRequest[] = [];
const MONOSPACE = Platform.select({ ios: "Menlo", default: "monospace" });

/**
 * Sheet stack above the composer: tucked behind its top as in the webapp, or
 * a floating glass card where the composer is liquid glass. The top sheet is
 * interactive, lower ones peek out above it. The stack reports its height to
 * the chat layout, so the transcript stays scrollable above it, and it
 * shrinks with the keyboard. Question and permission sheets fold into their
 * header to free the transcript; a new request unfolds them.
 */
export function ComposerSheetStack({
  sheets,
  gates,
  queue,
  onChangeTime,
  onSelectCandidate,
  testID = "composer-sheet-stack",
}: ComposerSheetStackProps): React.JSX.Element {
  const { onSheetsHeight, sheetMaxHeight } = useChatLayout();
  const top = sheets[0];
  const peeks = Math.max(0, Math.min(sheets.length - 1, MAX_PEEKS));
  const key = top ? sheetKey(top) : null;
  const [collapsedKey, setCollapsedKey] = useState<string | null>(null);
  const collapsed = key !== null && collapsedKey === key;
  const drafts = useQuestionDrafts(gates?.questions ?? NO_QUESTIONS);
  const sizeStyle = useAnimatedStyle(() => ({
    maxHeight: sheetMaxHeight.value,
  }));

  const onLayout = (event: LayoutChangeEvent) =>
    onSheetsHeight(
      top ? Math.max(0, event.nativeEvent.layout.height - OVERLAP) : 0,
    );
  const toggleCollapsed = () =>
    setCollapsedKey((current) => (current === key ? null : key));

  let content: React.ReactNode = null;
  if (top?.kind === "mention" && top.mention)
    content = (
      <MentionSheet
        candidates={top.mention.candidates}
        onSelect={onSelectCandidate}
      />
    );
  else if (top?.kind === "question" && top.questions && gates)
    content = (
      <QuestionSheet
        requests={top.questions}
        drafts={drafts}
        submitting={Boolean(gates.submitting)}
        onSubmit={gates.onSubmitQuestion}
        onSkip={gates.onSkipQuestion}
        collapsed={collapsed}
        onToggleCollapsed={toggleCollapsed}
      />
    );
  else if (top?.kind === "permission" && top.permissions && gates)
    content = (
      <PermissionSheet
        requests={top.permissions}
        resolving={Boolean(gates.resolving)}
        onResolve={gates.onResolvePermission}
        collapsed={collapsed}
        onToggleCollapsed={toggleCollapsed}
      />
    );
  else if (top?.kind === "queue" && queue)
    content = (
      <QueueSheet
        steers={top.queue ?? []}
        scheduled={top.scheduled ?? []}
        queue={queue}
        onChangeTime={onChangeTime}
      />
    );

  const shown = content !== null;
  // Without sheets the stack adds nothing, also before its next layout pass.
  useEffect(() => {
    if (!shown) onSheetsHeight(0);
  }, [onSheetsHeight, shown]);
  useEffect(() => () => onSheetsHeight(0), [onSheetsHeight]);

  return (
    <View
      testID={testID}
      collapsable={false}
      onLayout={onLayout}
      pointerEvents="box-none"
      style={content ? [styles.stack, { paddingTop: peeks * PEEK }] : undefined}
    >
      {content ? (
        <>
          {/* Farthest first, so nearer edges cover it. */}
          {sheets
            .slice(1, 1 + peeks)
            .map((sheet, index) => ({ sheet, depth: index + 1 }))
            .reverse()
            .map(({ sheet, depth }) => (
              <View
                key={sheet.kind}
                pointerEvents="none"
                testID="composer-sheet-peek"
                style={[
                  styles.peekSlot,
                  {
                    top: (peeks - depth) * PEEK,
                    left: peekInset(depth),
                    right: peekInset(depth),
                    // Glass shows what lies beneath it: only the edge stays.
                    height: composerSheetsFloat ? PEEK : PEEK_HEIGHT,
                  },
                ]}
              >
                <ComposerSheetPeek style={styles.peek} />
              </View>
            ))}
          <Reanimated.View style={[styles.top, sizeStyle]}>
            <ComposerSheetSurface style={styles.shrink}>
              <View
                testID="composer-sheet-top"
                style={[styles.shrink, styles.content]}
              >
                {content}
              </View>
            </ComposerSheetSurface>
          </Reanimated.View>
        </>
      ) : null}
    </View>
  );
}

/** The host reports failed resolutions; the sheet stays open for a retry. */
async function settle(action: () => void | Promise<void>): Promise<void> {
  try {
    await action();
  } catch {
    // Shown by the host, which owns the transport.
  }
}

function sheetKey(sheet: ComposerSheet): string {
  const request = sheet.question ?? sheet.permission;
  return request ? `${sheet.kind}:${request.request_id}` : sheet.kind;
}

interface QuestionDrafts {
  get: (request: HarnessQuestionRequest) => QuestionDraft;
  update: (
    request: HarnessQuestionRequest,
    change: (draft: QuestionDraft) => QuestionDraft,
  ) => void;
}

/**
 * Unsent answers per pending request. They live in the stack, so a mention
 * sheet covering the question keeps them; resolved requests drop theirs.
 */
function useQuestionDrafts(requests: HarnessQuestionRequest[]): QuestionDrafts {
  const [drafts, setDrafts] = useState<Record<string, QuestionDraft>>({});
  const ids = requests.map((request) => request.request_id).join("\n");
  useEffect(() => {
    const pending = new Set(ids.split("\n"));
    setDrafts((current) => {
      const kept = Object.entries(current).filter(([id]) => pending.has(id));
      return kept.length === Object.keys(current).length
        ? current
        : Object.fromEntries(kept);
    });
  }, [ids]);
  const get = useCallback(
    (request: HarnessQuestionRequest) =>
      drafts[request.request_id] ?? emptyQuestionDraft(request),
    [drafts],
  );
  const update = useCallback(
    (
      request: HarnessQuestionRequest,
      change: (draft: QuestionDraft) => QuestionDraft,
    ) =>
      setDrafts((current) => ({
        ...current,
        [request.request_id]: change(
          current[request.request_id] ?? emptyQuestionDraft(request),
        ),
      })),
    [],
  );
  return { get, update };
}

/** Pages through requests of one kind; resets when the first request changes. */
function usePage(firstId: string | undefined, total: number) {
  const [page, setPage] = useState(0);
  useEffect(() => setPage(0), [firstId]);
  return [Math.min(page, Math.max(total - 1, 0)), setPage] as const;
}

function SheetHeader({
  icon,
  title,
  badge,
  noun,
  page,
  total,
  onPage,
  busy,
  collapsed,
  onToggleCollapsed,
}: {
  icon: React.ReactNode;
  title: string;
  badge?: string | null;
  /** Singular noun for pager labels, e.g. "question". */
  noun: string;
  page: number;
  total: number;
  onPage: (page: number) => void;
  busy: boolean;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={styles.header}>
      {icon}
      <Text
        accessibilityRole="header"
        numberOfLines={1}
        style={[styles.title, { color: theme.text }]}
      >
        {title}
      </Text>
      {badge ? (
        <Text
          numberOfLines={1}
          style={[
            styles.badge,
            { color: theme.mutedText, borderColor: theme.border },
          ]}
        >
          {badge}
        </Text>
      ) : null}
      <View style={styles.spacer} />
      {total > 1 ? (
        <>
          <IconButton
            symbol="chevron.left"
            icon={<ChevronLeft size={18} color={theme.text} />}
            label={`Previous ${noun}`}
            size={40}
            disabled={busy || page === 0}
            onPress={() => onPage(Math.max(0, page - 1))}
            testID={`composer-${noun}-previous`}
          />
          <Text style={[styles.pageLabel, { color: theme.mutedText }]}>
            {page + 1} of {total}
          </Text>
          <IconButton
            symbol="chevron.right"
            icon={<ChevronRight size={18} color={theme.text} />}
            label={`Next ${noun}`}
            size={40}
            disabled={busy || page >= total - 1}
            onPress={() => onPage(Math.min(total - 1, page + 1))}
            testID={`composer-${noun}-next`}
          />
        </>
      ) : null}
      <IconButton
        symbol={collapsed ? "chevron.up" : "chevron.down"}
        icon={
          collapsed ? (
            <ChevronUp size={18} color={theme.mutedText} />
          ) : (
            <ChevronDown size={18} color={theme.mutedText} />
          )
        }
        label={
          collapsed
            ? `Show ${title.toLowerCase()}`
            : `Hide ${title.toLowerCase()}`
        }
        tone="quiet"
        size={40}
        onPress={onToggleCollapsed}
        testID={`composer-${noun}-collapse`}
      />
    </View>
  );
}

function QuestionSheet({
  requests,
  drafts,
  submitting,
  onSubmit,
  onSkip,
  collapsed,
  onToggleCollapsed,
}: {
  requests: HarnessQuestionRequest[];
  drafts: QuestionDrafts;
  submitting: boolean;
  onSubmit: ComposerGates["onSubmitQuestion"];
  onSkip: ComposerGates["onSkipQuestion"];
  collapsed: boolean;
  onToggleCollapsed: () => void;
}): React.JSX.Element | null {
  const { theme } = useTheme();
  const [page, setPage] = usePage(requests[0]?.request_id, requests.length);
  const inView = useAnswerInView();
  const request = requests[page];
  if (!request) return null;
  const draft = drafts.get(request);
  const numbered = request.questions.length > 1;
  const answers = collectQuestionAnswers(request, draft);
  // Nothing to send yet: Skip is the way out.
  const answered = answers.some((answer) => answer.trim().length > 0);

  const run = (action: () => void | Promise<void>) => {
    if (!submitting) void settle(action);
  };

  return (
    <View testID="composer-question-sheet" style={styles.shrink}>
      <SheetHeader
        icon={<MessageCircleQuestion size={16} color={theme.mutedText} />}
        title="Questions"
        badge={gateSourceLabel(request.agent_name)}
        noun="question"
        page={page}
        total={requests.length}
        onPage={setPage}
        busy={submitting}
        collapsed={collapsed}
        onToggleCollapsed={onToggleCollapsed}
      />
      {collapsed ? null : (
        <>
          <ScrollView
            ref={inView.scroll}
            onLayout={inView.onViewportLayout}
            style={styles.shrink}
            contentContainerStyle={styles.questions}
            keyboardShouldPersistTaps="handled"
            nestedScrollEnabled
            scrollsToTop={false}
          >
            {request.questions.map((question, index) => {
              const selected = selectedQuestionOptions(draft, index);
              const hasOptions = Boolean(question.options?.length);
              const caption = [
                question.header,
                hasOptions && question.multiple ? "Select all that apply" : "",
              ]
                .filter(Boolean)
                .join(" · ");
              return (
                <View
                  key={`${request.request_id}-${index}`}
                  onLayout={(event) => inView.onQuestionLayout(index, event)}
                  style={styles.question}
                >
                  <View style={styles.prompt}>
                    {caption ? (
                      <Text
                        style={[styles.caption, { color: theme.mutedText }]}
                      >
                        {caption}
                      </Text>
                    ) : null}
                    <Text
                      accessibilityRole="header"
                      style={[styles.questionText, { color: theme.text }]}
                    >
                      {numbered ? (
                        <Text style={{ color: theme.subtleText }}>
                          {index + 1}.{" "}
                        </Text>
                      ) : null}
                      {question.question}
                    </Text>
                  </View>
                  <View style={styles.choices}>
                    {question.options?.map((option) => {
                      const picked = selected.includes(option.label);
                      return (
                        <ChoiceCard key={option.label} active={picked}>
                          <NativeOption
                            label={option.label}
                            description={option.description}
                            selected={picked}
                            multiple={question.multiple}
                            disabled={submitting}
                            onPress={() =>
                              drafts.update(request, (current) =>
                                toggleQuestionOption(
                                  current,
                                  request,
                                  index,
                                  option.label,
                                ),
                              )
                            }
                          />
                        </ChoiceCard>
                      );
                    })}
                    <CustomAnswer
                      question={question.question}
                      hasOptions={hasOptions}
                      value={draft.custom[index] ?? ""}
                      active={isQuestionCustomActive(draft, request, index)}
                      disabled={submitting}
                      onChangeText={(value) =>
                        drafts.update(request, (current) =>
                          setQuestionCustomAnswer(
                            current,
                            request,
                            index,
                            value,
                          ),
                        )
                      }
                      onFocus={() => inView.onFocus(index)}
                      onBlur={inView.onBlur}
                    />
                  </View>
                </View>
              );
            })}
          </ScrollView>
          <View style={styles.actions}>
            <Button
              title="Skip"
              variant="secondary"
              shape="capsule"
              disabled={submitting}
              onPress={() => {
                run(() => onSkip(request.request_id));
              }}
              style={styles.action}
              testID="composer-question-skip"
            />
            <Button
              title="Continue"
              variant="primary"
              shape="capsule"
              busy={submitting}
              disabled={!answered}
              onPress={() => {
                run(() => onSubmit(request.request_id, answers));
              }}
              style={styles.action}
              testID="composer-question-submit"
            />
          </View>
        </>
      )}
    </View>
  );
}

/**
 * Keeps a focused free-text answer in view while the sheet shrinks above the
 * keyboard. The answer row ends its question, so the question's lower edge
 * stays at the bottom of the visible area, also as the sheet keeps resizing.
 */
function useAnswerInView() {
  const scroll = useRef<ScrollView>(null);
  const questions = useRef<{ y: number; height: number }[]>([]);
  const viewport = useRef(0);
  const focused = useRef<number | null>(null);
  const reveal = useCallback((animated: boolean) => {
    const frame =
      focused.current === null ? undefined : questions.current[focused.current];
    if (!frame) return;
    scroll.current?.scrollTo({
      y: Math.max(0, frame.y + frame.height - viewport.current),
      animated,
    });
  }, []);
  return {
    scroll,
    onViewportLayout: (event: LayoutChangeEvent) => {
      viewport.current = event.nativeEvent.layout.height;
      reveal(false);
    },
    onQuestionLayout: (index: number, event: LayoutChangeEvent) => {
      questions.current[index] = event.nativeEvent.layout;
    },
    onFocus: (index: number) => {
      focused.current = index;
      // After the keyboard, which shrinks the sheet, has come up.
      setTimeout(() => reveal(true), 300);
    },
    onBlur: () => {
      focused.current = null;
    },
  };
}

/** Rounded tile of one choice; a picked choice is outlined in the ink. */
function ChoiceCard({
  active,
  children,
}: React.PropsWithChildren<{ active: boolean }>): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View
      style={[
        styles.choice,
        {
          backgroundColor: theme.controlFill,
          borderColor: active ? theme.primary : "transparent",
        },
      ]}
    >
      {children}
    </View>
  );
}

/**
 * Free-text answer, the last choice beneath the options: its pencil lines up
 * with their indicators. Typing replaces a single choice (chat-core rules).
 */
function CustomAnswer({
  question,
  hasOptions,
  value,
  active,
  disabled,
  onChangeText,
  onFocus,
  onBlur,
}: {
  question: string;
  hasOptions: boolean;
  value: string;
  active: boolean;
  disabled: boolean;
  onChangeText: (value: string) => void;
  onFocus: () => void;
  onBlur: () => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <ChoiceCard active={active}>
      <View testID="composer-question-custom-row" style={styles.custom}>
        <View style={styles.customIcon}>
          <PenLine
            size={18}
            strokeWidth={2}
            color={active ? theme.text : theme.subtleText}
          />
        </View>
        <TextInput
          accessibilityLabel={`Your answer for ${question}`}
          placeholder={hasOptions ? "Something else…" : "Your answer…"}
          placeholderTextColor={theme.subtleText}
          value={value}
          editable={!disabled}
          multiline
          autoCorrect
          spellCheck
          autoCapitalize="sentences"
          submitBehavior="blurAndSubmit"
          returnKeyType="done"
          textAlignVertical="top"
          selectionColor={theme.primary}
          onChangeText={onChangeText}
          onFocus={onFocus}
          onBlur={onBlur}
          style={[styles.customInput, { color: theme.text }]}
          testID="composer-question-custom"
        />
      </View>
    </ChoiceCard>
  );
}

function PermissionSheet({
  requests,
  resolving,
  onResolve,
  collapsed,
  onToggleCollapsed,
}: {
  requests: HarnessPermissionRequest[];
  resolving: boolean;
  onResolve: ComposerGates["onResolvePermission"];
  collapsed: boolean;
  onToggleCollapsed: () => void;
}): React.JSX.Element | null {
  const { theme } = useTheme();
  const [page, setPage] = usePage(requests[0]?.request_id, requests.length);
  const request = requests[page];
  if (!request) return null;
  const source = gateSourceLabel(request.agent_name);

  const resolve = (response: HarnessPermissionResponse) => {
    if (!resolving) void settle(() => onResolve(request.request_id, response));
  };

  return (
    <View testID="composer-permission-sheet" style={styles.shrink}>
      <SheetHeader
        icon={<ShieldAlert size={16} color={theme.warning} />}
        title="Permission required"
        noun="permission"
        page={page}
        total={requests.length}
        onPage={setPage}
        busy={resolving}
        collapsed={collapsed}
        onToggleCollapsed={onToggleCollapsed}
      />
      {collapsed ? null : (
        <>
          <ScrollView
            style={styles.shrink}
            contentContainerStyle={styles.body}
            nestedScrollEnabled
            scrollsToTop={false}
          >
            <Text style={[styles.hint, { color: theme.mutedText }]}>
              {source
                ? `The ${source} wants to run a tool that needs approval.`
                : "The agent wants to run a tool that needs approval."}
            </Text>
            <View style={styles.permissionTitle}>
              <Text
                style={[
                  styles.tool,
                  { color: theme.text, backgroundColor: theme.surfaceMuted },
                ]}
              >
                {request.tool}
              </Text>
              <Text
                style={[
                  styles.questionText,
                  styles.spacer,
                  { color: theme.text },
                ]}
              >
                {request.title}
              </Text>
            </View>
            {request.pattern ? (
              <View
                style={[styles.code, { backgroundColor: theme.codeBackground }]}
              >
                <Text
                  selectable
                  style={[styles.codeText, { color: theme.text }]}
                >
                  {request.pattern}
                </Text>
              </View>
            ) : null}
          </ScrollView>
          <View style={styles.actions}>
            <Button
              title="Reject"
              shape="capsule"
              variant="outline"
              disabled={resolving}
              onPress={() => {
                resolve("reject");
              }}
              style={styles.action}
            />
            <Button
              title="Approve once"
              shape="capsule"
              variant="secondary"
              disabled={resolving}
              onPress={() => {
                resolve("once");
              }}
              style={styles.action}
            />
          </View>
          <View style={styles.actions}>
            <Button
              title="Always allow"
              shape="capsule"
              variant="primary"
              busy={resolving}
              onPress={() => {
                resolve("always");
              }}
              style={styles.action}
            />
          </View>
        </>
      )}
    </View>
  );
}

/** Single-line preview: line breaks and runs of whitespace collapse. */
function preview(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/**
 * Outgoing messages: steers waiting for the agent's next step, then messages
 * scheduled for later. A scheduled row opens its actions as a native menu.
 */
function QueueSheet({
  steers,
  scheduled,
  queue,
  onChangeTime,
}: {
  steers: HarnessSteer[];
  scheduled: HarnessScheduledMessage[];
  queue: ComposerQueue;
  onChangeTime?: (message: HarnessScheduledMessage) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <ScrollView
      accessibilityLabel="Outgoing messages"
      testID="composer-queue-sheet"
      style={[styles.shrink, { maxHeight: 216 }]}
      contentContainerStyle={styles.queue}
      keyboardShouldPersistTaps="handled"
      nestedScrollEnabled
      scrollsToTop={false}
    >
      {steers.map((steer) => (
        <View
          key={steer.id}
          testID="composer-queue-row"
          style={styles.queueRow}
        >
          <Clock size={17} color={theme.mutedText} />
          <QueueText caption="Queued for the next step" text={steer.content} />
          <IconButton
            symbol="xmark"
            icon={<X size={17} color={theme.mutedText} />}
            label="Withdraw queued message"
            tone="quiet"
            size={40}
            onPress={() => {
              void settle(() => queue.onWithdrawSteer(steer.id));
            }}
            testID="composer-queue-withdraw"
          />
        </View>
      ))}
      {scheduled.map((message) => (
        <ScheduledRow
          key={message.id}
          message={message}
          queue={queue}
          onChangeTime={onChangeTime}
        />
      ))}
    </ScrollView>
  );
}

function ScheduledRow({
  message,
  queue,
  onChangeTime,
}: {
  message: HarnessScheduledMessage;
  queue: ComposerQueue;
  onChangeTime?: (message: HarnessScheduledMessage) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const sending = message.status === "sending";
  const failed = message.status === "failed";
  const when = formatSendTime(message.send_at);
  const caption = sending
    ? "Sending…"
    : failed
      ? `Not sent · ${message.error || when}`
      : when;
  const icon = sending ? (
    <ActivityIndicator size="small" color={theme.mutedText} />
  ) : failed ? (
    <CircleAlert size={17} color={theme.danger} />
  ) : (
    <CalendarClock size={17} color={theme.mutedText} />
  );
  const content = (
    <View testID="composer-scheduled-row" style={styles.queueRow}>
      {icon}
      <QueueText
        caption={caption}
        captionColor={failed ? theme.danger : undefined}
        text={message.content}
      />
      {sending ? null : (
        <View style={styles.queueMore}>
          <Ellipsis size={18} color={theme.mutedText} />
        </View>
      )}
    </View>
  );
  if (sending) return content;
  return (
    <NativeMenu
      label={failed ? "Not sent" : `Scheduled · ${when}`}
      accessibilityLabel={`${caption}: ${preview(message.content)}`}
      testID={`composer-scheduled-menu-${message.id}`}
      actions={[
        {
          id: "send",
          title: failed ? "Try again now" : "Send now",
          symbol: "paperplane",
          disabled: queue.canSendNow === false,
        },
        ...(onChangeTime
          ? [{ id: "time", title: "Change time", symbol: "clock" }]
          : []),
        { id: "edit", title: "Edit", symbol: "pencil" },
        { id: "delete", title: "Delete", symbol: "trash", destructive: true },
      ]}
      onSelect={(id) => {
        if (id === "send") void settle(() => queue.onSendNow(message.id));
        else if (id === "time") onChangeTime?.(message);
        else if (id === "edit") void settle(() => queue.onEdit(message.id));
        else if (id === "delete") void settle(() => queue.onDelete(message.id));
      }}
    >
      {content}
    </NativeMenu>
  );
}

function QueueText({
  caption,
  captionColor,
  text,
}: {
  caption: string;
  captionColor?: string;
  text: string;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={styles.queueText}>
      <Text
        numberOfLines={1}
        style={[
          styles.queueCaption,
          { color: captionColor ?? theme.mutedText },
        ]}
      >
        {caption}
      </Text>
      <Text numberOfLines={1} style={[styles.queueBody, { color: theme.text }]}>
        {preview(text)}
      </Text>
    </View>
  );
}

function MentionSheet({
  candidates,
  onSelect,
}: {
  candidates: MentionCandidate[];
  onSelect: (candidate: MentionCandidate) => void;
}): React.JSX.Element {
  const { theme } = useTheme();
  const skills = candidates[0]?.kind === "skill";
  return (
    <View
      accessibilityLabel={`${skills ? "Skill" : "Context"} suggestions`}
      testID="composer-mention-sheet"
      style={styles.shrink}
    >
      <View style={styles.mentionTitle}>
        <Text style={[styles.mentionHeading, { color: theme.mutedText }]}>
          {skills ? "Skills" : "Add context"}
        </Text>
        <Text style={{ color: theme.subtleText, fontSize: 12 }}>
          Select to add
        </Text>
      </View>
      <ScrollView
        keyboardShouldPersistTaps="handled"
        scrollsToTop={false}
        style={[styles.shrink, { maxHeight: 192 }]}
      >
        {candidates.slice(0, 10).map((candidate) => (
          <NativeAction
            key={`${candidate.kind}:${candidate.insert}`}
            accessibilityRole="button"
            accessibilityLabel={`Add ${candidate.label}`}
            onPress={() => onSelect(candidate)}
            style={({ pressed }) => [
              styles.mentionItem,
              {
                backgroundColor: pressed ? theme.surfaceMuted : "transparent",
              },
            ]}
          >
            {candidate.kind === "skill" ? (
              <Sparkles size={16} color={theme.primary} />
            ) : candidate.kind === "agent" ? (
              <AtSign size={16} color={theme.primary} />
            ) : (
              <FileText size={16} color={theme.mutedText} />
            )}
            <Text
              numberOfLines={1}
              style={{ flex: 1, color: theme.text, fontSize: 14 }}
            >
              {candidate.label}
            </Text>
            <Plus size={16} color={theme.subtleText} />
          </NativeAction>
        ))}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  // A tucked sheet is narrower than the composer, whose top edge overlaps
  // it; a floating card is as wide as the composer.
  stack: {
    marginHorizontal: composerSheetsFloat ? 0 : 10,
    marginBottom: -OVERLAP,
  },
  peekSlot: { position: "absolute", overflow: "hidden" },
  peek: { height: PEEK_HEIGHT },
  top: { minHeight: 0 },
  shrink: { flexShrink: 1, minHeight: 0 },
  content: { paddingTop: 4, paddingBottom: Math.max(OVERLAP, 0) + 12 },
  header: {
    minHeight: 44,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingLeft: 16,
    paddingRight: 6,
  },
  title: { flexShrink: 1, fontSize: 15, fontWeight: "600" },
  badge: {
    flexShrink: 1,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 8,
    paddingHorizontal: 6,
    paddingVertical: 1,
    fontSize: 12,
    overflow: "hidden",
  },
  spacer: { flex: 1 },
  pageLabel: { fontSize: 13, fontVariant: ["tabular-nums"] },
  body: { gap: 18, paddingHorizontal: 16, paddingTop: 2, paddingBottom: 10 },
  questions: { gap: 24, paddingHorizontal: 12, paddingTop: 2, paddingBottom: 4 },
  question: { gap: 12 },
  prompt: { gap: 3, paddingHorizontal: 4 },
  caption: { fontSize: 13, lineHeight: 18, fontWeight: "500" },
  questionText: { fontSize: 17, lineHeight: 22, fontWeight: "600" },
  hint: { fontSize: 13, lineHeight: 18 },
  choices: { gap: 8 },
  choice: {
    borderRadius: 16,
    borderCurve: "continuous",
    borderWidth: 1.5,
    overflow: "hidden",
  },
  // As tall as a one-line option row, its text in the label's size.
  custom: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: OPTION_ROW.gap,
    paddingHorizontal: OPTION_ROW.inset,
    paddingVertical: (OPTION_ROW.minHeight - OPTION_ROW.lineHeight) / 2,
  },
  customIcon: {
    width: OPTION_ROW.indicator,
    height: OPTION_ROW.lineHeight,
    alignItems: "center",
    justifyContent: "center",
  },
  customInput: {
    flex: 1,
    minHeight: OPTION_ROW.lineHeight,
    maxHeight: OPTION_ROW.lineHeight * 5,
    padding: 0,
    paddingTop: 0,
    paddingBottom: 0,
    fontSize: OPTION_ROW.fontSize,
    lineHeight: OPTION_ROW.lineHeight,
  },
  actions: {
    flexDirection: "row",
    gap: 10,
    paddingHorizontal: 12,
    paddingTop: 12,
  },
  action: { flex: 1 },
  permissionTitle: { flexDirection: "row", alignItems: "center", gap: 8 },
  tool: {
    borderRadius: 8,
    paddingHorizontal: 7,
    paddingVertical: 2,
    fontFamily: MONOSPACE,
    fontSize: 12,
    overflow: "hidden",
  },
  code: { borderRadius: 12, borderCurve: "continuous", padding: 12 },
  codeText: { fontFamily: MONOSPACE, fontSize: 12, lineHeight: 18 },
  mentionTitle: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingHorizontal: 16,
    paddingTop: 10,
    paddingBottom: 6,
  },
  mentionHeading: { fontSize: 12, fontWeight: "600" },
  mentionItem: {
    minHeight: 46,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingHorizontal: 16,
  },
  queue: { paddingVertical: 2 },
  queueRow: {
    minHeight: 52,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingLeft: 16,
    paddingRight: 6,
  },
  queueText: { flex: 1, minWidth: 0, gap: 1 },
  queueCaption: { fontSize: 12, lineHeight: 16, fontVariant: ["tabular-nums"] },
  queueBody: { fontSize: 15, lineHeight: 20 },
  queueMore: {
    width: 40,
    height: 40,
    alignItems: "center",
    justifyContent: "center",
  },
});
