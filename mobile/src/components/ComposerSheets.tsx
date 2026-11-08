import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type LayoutChangeEvent,
} from "react-native";
import Reanimated, { useAnimatedStyle } from "react-native-reanimated";
import {
  AtSign,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  FileText,
  MessageCircleQuestion,
  Plus,
  ShieldAlert,
  Sparkles,
} from "lucide-react-native";
import type { ComposerSheet } from "@opencuria/chat-core/composerSheets";
import type {
  HarnessPermissionRequest,
  HarnessPermissionResponse,
  HarnessQuestionRequest,
} from "@opencuria/chat-core/harness";
import type { MentionCandidate } from "@opencuria/chat-core/harnessMentions";
import {
  collectQuestionAnswers,
  emptyQuestionDraft,
  selectedQuestionOptions,
  setQuestionCustomAnswer,
  toggleQuestionOption,
  type QuestionDraft,
} from "@opencuria/chat-core/harnessQuestion";
import { gateSourceLabel } from "@opencuria/chat-core/harnessSubtaskActivity";
import { ComposerSheetSurface } from "../ui/ComposerSurface";
import { NativeTextField } from "../ui/NativeTextField";
import { Button, IconButton, NativeAction } from "../ui/primitives";
import { NativeOption } from "../ui/selection";
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

export interface ComposerSheetStackProps {
  /** Ordered by `buildComposerSheets`; only the first sheet is interactive. */
  sheets: ComposerSheet[];
  gates?: ComposerGates;
  onSelectCandidate: (candidate: MentionCandidate) => void;
  testID?: string;
}

/** The composer covers this much of the sheet's lower edge. */
const OVERLAP = 24;
/** Visible edge of each sheet waiting beneath the top one. */
const PEEK = 7;
const MAX_PEEKS = 2;
const NO_QUESTIONS: HarnessQuestionRequest[] = [];
const MONOSPACE = Platform.select({ ios: "Menlo", default: "monospace" });

/**
 * Sheet stack tucked behind the top of the composer, as in the webapp: the
 * top sheet is interactive, lower ones peek out above it. The stack reports
 * its height to the chat layout, so the transcript stays scrollable above it,
 * and it shrinks with the keyboard. Question and permission sheets fold into
 * their header to free the transcript; a new request unfolds them.
 */
export function ComposerSheetStack({
  sheets,
  gates,
  onSelectCandidate,
  testID = "composer-sheet-stack",
}: ComposerSheetStackProps): React.JSX.Element {
  const { theme } = useTheme();
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
                  styles.peek,
                  {
                    top: (peeks - depth) * PEEK,
                    left: depth * 9,
                    right: depth * 9,
                    backgroundColor: theme.surfaceMuted,
                    borderColor: theme.border,
                  },
                ]}
              />
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
  const scroll = useRef<ScrollView>(null);
  const offsets = useRef<number[]>([]);
  const request = requests[page];
  if (!request) return null;
  const draft = drafts.get(request);
  const numbered = request.questions.length > 1;

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
            ref={scroll}
            style={styles.shrink}
            contentContainerStyle={styles.body}
            keyboardShouldPersistTaps="handled"
            nestedScrollEnabled
            scrollsToTop={false}
          >
            {request.questions.map((question, index) => {
              const selected = selectedQuestionOptions(draft, index);
              const hasOptions = Boolean(question.options?.length);
              return (
                <View
                  key={`${request.request_id}-${index}`}
                  onLayout={(event) => {
                    offsets.current[index] = event.nativeEvent.layout.y;
                  }}
                  style={styles.question}
                >
                  {question.header ? (
                    <Text style={[styles.label, { color: theme.subtleText }]}>
                      {question.header}
                    </Text>
                  ) : null}
                  <Text style={[styles.questionText, { color: theme.text }]}>
                    {numbered ? (
                      <Text style={{ color: theme.subtleText }}>
                        {index + 1}.{" "}
                      </Text>
                    ) : null}
                    {question.question}
                  </Text>
                  {hasOptions ? (
                    <>
                      <Text style={[styles.hint, { color: theme.mutedText }]}>
                        {question.multiple
                          ? "Select all that apply"
                          : "Select one"}
                      </Text>
                      <View
                        style={[
                          styles.options,
                          { backgroundColor: theme.surfaceMuted },
                        ]}
                      >
                        {question.options?.map((option) => (
                          <NativeOption
                            key={option.label}
                            label={option.label}
                            description={option.description}
                            selected={selected.includes(option.label)}
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
                        ))}
                      </View>
                    </>
                  ) : null}
                  <NativeTextField
                    label="Your answer"
                    accessibilityLabel={`Your answer for ${question.question}`}
                    placeholder={hasOptions ? "Own answer…" : "Your answer…"}
                    value={draft.custom[index] ?? ""}
                    prose
                    onChangeText={(value) =>
                      drafts.update(request, (current) =>
                        setQuestionCustomAnswer(current, request, index, value),
                      )
                    }
                    // The sheet shrinks above the keyboard; keep the field in view.
                    onFocus={() =>
                      setTimeout(
                        () =>
                          scroll.current?.scrollTo({
                            y: offsets.current[index] ?? 0,
                            animated: true,
                          }),
                        300,
                      )
                    }
                    testID="composer-question-custom"
                  />
                </View>
              );
            })}
          </ScrollView>
          <View style={styles.actions}>
            <Button
              title="Skip"
              variant="ghost"
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
              busy={submitting}
              onPress={() => {
                run(() =>
                  onSubmit(
                    request.request_id,
                    collectQuestionAnswers(request, drafts.get(request)),
                  ),
                );
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
              variant="outline"
              disabled={resolving}
              onPress={() => {
                resolve("reject");
              }}
              style={styles.action}
            />
            <Button
              title="Approve once"
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
  // Narrower than the composer, whose top edge overlaps the sheet.
  stack: { marginHorizontal: 10, marginBottom: -OVERLAP },
  peek: {
    position: "absolute",
    height: PEEK * (MAX_PEEKS + 1) + 22,
    borderRadius: 20,
    borderCurve: "continuous",
    borderWidth: StyleSheet.hairlineWidth,
  },
  top: { minHeight: 0 },
  shrink: { flexShrink: 1, minHeight: 0 },
  content: { paddingTop: 4, paddingBottom: OVERLAP + 8 },
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
  question: { gap: 8 },
  label: {
    fontSize: 11,
    lineHeight: 15,
    letterSpacing: 0.7,
    fontWeight: "600",
    textTransform: "uppercase",
  },
  questionText: { fontSize: 16, lineHeight: 22, fontWeight: "600" },
  hint: { fontSize: 13, lineHeight: 18 },
  options: {
    borderRadius: 14,
    borderCurve: "continuous",
    overflow: "hidden",
    paddingHorizontal: Platform.OS === "ios" ? 14 : 0,
  },
  actions: {
    flexDirection: "row",
    gap: 8,
    paddingHorizontal: 12,
    paddingTop: 4,
    paddingBottom: 4,
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
});
