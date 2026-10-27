import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Image } from "expo-image";
import {
  AtSign,
  ArrowUp,
  FileText,
  Plus,
  Sparkles,
  Square,
  X,
} from "lucide-react-native";
import {
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
  useWindowDimensions,
  type NativeSyntheticEvent,
  type TextInputSelectionChangeEventData,
} from "react-native";
import {
  applyMentionCandidate,
  consumeSlashQuery,
  detectMentionQuery,
  detectSlashQuery,
  filterMentionCandidates,
  filterSkillCandidates,
  type MentionCandidate,
} from "@opencuria/chat-core/harnessMentions";
import { Banner, IconButton, NativeAction } from "../ui/primitives";
import { NativeMenu } from "../ui/NativeMenu";
import { ComposerSurface } from "../ui/ComposerSurface";
import { ComposerEditor } from "./ComposerEditor";
import {
  COMPOSER_INPUT_NATIVE_ID,
  ComposerDismissArea,
  useChatLayout,
} from "./ChatLayout";
import { useTheme } from "../ui/theme";

export interface ComposerSkill {
  id: string;
  name: string;
  description?: string;
}
export interface ComposerAttachment {
  id: string;
  uri: string;
  name: string;
  mimeType?: string;
  size?: number;
  kind?: "image" | "video" | "file";
}
export interface ComposerSettingsSummary {
  harness?: string;
  mode?: "plan" | "build" | string;
  model?: string;
  effort?: string;
}

export interface ChatComposerProps {
  draft: string;
  onDraftChange: (
    draft: string,
    selection: { start: number; end: number },
  ) => void;
  onSend: (value: {
    text: string;
    skillIds: string[];
    attachments: ComposerAttachment[];
  }) => void | Promise<void>;
  onStop?: () => void | Promise<void>;
  busy?: boolean;
  disabled?: boolean;
  placeholder?: string;
  /** Summary shown on the model control; pressing it calls `onSettings`. */
  settings?: ComposerSettingsSummary;
  onSettings?: () => void;
  skills?: ComposerSkill[];
  selectedSkillIds?: string[];
  onSelectedSkillIdsChange?: (ids: string[]) => void;
  attachments?: ComposerAttachment[];
  onAttachmentsChange?: (files: ComposerAttachment[]) => void;
  /** Optional parent-owned asynchronous workspace file lookup for `@` queries. */
  onSearchMentions?: (query: string) => Promise<MentionCandidate[]>;
  /** Parent may pass freshly fetched mention/skill results while a query is active. */
  proposals?: MentionCandidate[];
  /** Changes when the owning chat/workspace context changes; invalidates transient work. */
  contextKey?: string | number;
  /** Native platform adapters return selected records; the parent owns permissions and upload. */
  onTakePhoto?: () => Promise<ComposerAttachment | ComposerAttachment[] | null>;
  onChoosePhotos?: () => Promise<
    ComposerAttachment | ComposerAttachment[] | null
  >;
  onChooseFiles?: () => Promise<
    ComposerAttachment | ComposerAttachment[] | null
  >;
  testID?: string;
}

interface ActiveQuery {
  kind: "mention" | "skill";
  query: string;
}

/** Shared empty result, so clearing an empty list does not re-render. */
const NO_CANDIDATES: MentionCandidate[] = [];

/**
 * Controlled native composer. Native editing keeps its own caret; the parent
 * receives the best synchronous selection estimate and native selection events
 * correct it. Only candidate insertion explicitly moves the caret. Network,
 * picker, context reset, and transport lifecycle are owned by the host.
 */
export function ChatComposer({
  draft,
  onDraftChange,
  onSend,
  onStop,
  busy = false,
  disabled = false,
  placeholder = "Ask OpenCuria",
  settings,
  onSettings,
  skills = [],
  selectedSkillIds = [],
  onSelectedSkillIdsChange,
  attachments = [],
  onAttachmentsChange,
  onSearchMentions,
  proposals,
  contextKey,
  onTakePhoto,
  onChoosePhotos,
  onChooseFiles,
  testID = "chat-composer",
}: ChatComposerProps): React.JSX.Element {
  const { theme } = useTheme();
  const { width: windowWidth } = useWindowDimensions();
  const { dismissKeyboard, onComposerLayout } = useChatLayout();
  const [query, setQuery] = useState<ActiveQuery | null>(null);
  const [searched, setSearched] = useState<MentionCandidate[]>(NO_CANDIDATES);
  const [inputScrolls, setInputScrolls] = useState(false);
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const sendingRef = useRef(false);
  const stopInFlight = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<TextInput>(null);
  // Selection lives in refs: re-rendering on focus would race the keyboard
  // animation, and the native input owns the caret anyway.
  const selectionRef = useRef({ start: draft.length, end: draft.length });
  const nativeSelectionRef = useRef(false);
  const nativeEditTextRef = useRef<string | null>(null);
  const draftRef = useRef(draft);
  const attachmentsRef = useRef(attachments);
  const pickerInFlight = useRef(false);
  const queryGeneration = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const operationGeneration = useRef(0);
  const mounted = useRef(true);
  const contextKeyRef = useRef(contextKey);

  attachmentsRef.current = attachments;

  const activeSkills = useMemo(
    () =>
      selectedSkillIds.map((id) => {
        const skill = skills.find((item) => item.id === id);
        return skill ?? { id, name: id };
      }),
    [selectedSkillIds, skills],
  );

  const candidates = useMemo(() => {
    if (!query) return [];
    if (proposals) return proposals;
    if (query.kind === "skill")
      return filterSkillCandidates(query.query, skills);
    const matchedFiles = searched.filter(
      (candidate) => candidate.kind === "file",
    );
    const agentCandidates = filterMentionCandidates(query.query, []);
    return [...agentCandidates, ...matchedFiles].slice(0, 10);
  }, [proposals, query, searched, skills]);

  const cancelSearch = useCallback(() => {
    queryGeneration.current += 1;
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = null;
  }, []);

  const refreshQuery = useCallback(
    (text: string, cursor: number) => {
      cancelSearch();
      const mention = detectMentionQuery(text, cursor);
      if (mention !== null) {
        setQuery({ kind: "mention", query: mention });
        setSearched(NO_CANDIDATES);
        if (onSearchMentions && !mention.toLowerCase().startsWith("agent:")) {
          const generation = queryGeneration.current;
          timerRef.current = setTimeout(() => {
            timerRef.current = null;
            void onSearchMentions(mention)
              .then((next) => {
                if (mounted.current && generation === queryGeneration.current)
                  setSearched(next);
              })
              .catch(() => {
                if (mounted.current && generation === queryGeneration.current)
                  setSearched(NO_CANDIDATES);
              });
          }, 150);
        }
        return;
      }
      const slash = detectSlashQuery(text, cursor);
      if (slash !== null) {
        setSearched(NO_CANDIDATES);
        setQuery({ kind: "skill", query: slash });
      } else {
        setQuery(null);
        setSearched(NO_CANDIDATES);
      }
    },
    [cancelSearch, onSearchMentions],
  );

  const changeText = useCallback(
    (text: string) => {
      const nativeSelectionIsForThisEdit =
        nativeSelectionRef.current &&
        nativeEditTextRef.current === draftRef.current;
      const nextSelection = nativeSelectionIsForThisEdit
        ? selectionRef.current
        : estimateSelectionAfterEdit(
            draftRef.current,
            text,
            selectionRef.current,
          );
      nativeSelectionRef.current = false;
      nativeEditTextRef.current = null;
      draftRef.current = text;
      selectionRef.current = nextSelection;
      setError(null);
      onDraftChange(text, nextSelection);
      refreshQuery(text, nextSelection.end);
    },
    [onDraftChange, refreshQuery],
  );

  const selectCandidate = useCallback(
    (candidate: MentionCandidate) => {
      if (!query) return;
      const currentSelection = selectionRef.current;
      const result =
        candidate.kind === "skill"
          ? consumeSlashQuery(draftRef.current, currentSelection.end)
          : applyMentionCandidate(
              draftRef.current,
              currentSelection.end,
              candidate,
            );
      if (
        candidate.kind === "skill" &&
        !selectedSkillIds.includes(candidate.insert)
      ) {
        onSelectedSkillIdsChange?.([...selectedSkillIds, candidate.insert]);
      }
      draftRef.current = result.text;
      const nextSelection = { start: result.cursor, end: result.cursor };
      nativeSelectionRef.current = false;
      nativeEditTextRef.current = null;
      selectionRef.current = nextSelection;
      setQuery(null);
      setSearched(NO_CANDIDATES);
      setError(null);
      cancelSearch();
      onDraftChange(result.text, nextSelection);
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        inputRef.current?.setNativeProps({ selection: nextSelection });
      });
    },
    [
      cancelSearch,
      onDraftChange,
      onSelectedSkillIdsChange,
      query,
      selectedSkillIds,
    ],
  );

  useEffect(() => {
    if (contextKeyRef.current === contextKey) return;
    contextKeyRef.current = contextKey;
    nativeSelectionRef.current = false;
    nativeEditTextRef.current = null;
    operationGeneration.current += 1;
    sendingRef.current = false;
    stopInFlight.current = false;
    setSending(false);
    setStopping(false);
    setQuery(null);
    setSearched(NO_CANDIDATES);
    setError(null);
    cancelSearch();
  }, [cancelSearch, contextKey]);

  useEffect(() => {
    if (nativeSelectionRef.current) {
      if (draftRef.current !== draft) {
        draftRef.current = draft;
        nativeEditTextRef.current = draft;
      }
      return;
    }
    if (draftRef.current === draft) return;
    // Parent reset/prefill is distinct from a native edit; never leave a stale
    // @/slash proposal anchored to a prior draft or impose its old selection.
    operationGeneration.current += 1;
    sendingRef.current = false;
    setSending(false);
    draftRef.current = draft;
    nativeSelectionRef.current = false;
    const next = { start: draft.length, end: draft.length };
    nativeEditTextRef.current = null;
    selectionRef.current = next;
    setQuery(null);
    setSearched(NO_CANDIDATES);
    setError(null);
    cancelSearch();
  }, [cancelSearch, draft]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      operationGeneration.current += 1;
      sendingRef.current = false;
      stopInFlight.current = false;
      cancelSearch();
    };
  }, [cancelSearch]);

  const onSelectionChange = useCallback(
    (event: NativeSyntheticEvent<TextInputSelectionChangeEventData>) => {
      const next = event.nativeEvent.selection;
      nativeSelectionRef.current = true;
      nativeEditTextRef.current = draftRef.current;
      selectionRef.current = next;
      refreshQuery(draftRef.current, next.end);
    },
    [refreshQuery],
  );

  const addAttachments = useCallback(
    async (
      picker?:
        | ChatComposerProps["onTakePhoto"]
        | ChatComposerProps["onChoosePhotos"]
        | ChatComposerProps["onChooseFiles"],
    ) => {
      if (!picker || disabled || pickerInFlight.current) return;
      const generation = operationGeneration.current;
      pickerInFlight.current = true;
      const context = contextKeyRef.current;
      setError(null);
      try {
        const picked = await picker();
        if (
          !mounted.current ||
          generation !== operationGeneration.current ||
          context !== contextKeyRef.current ||
          !picked
        )
          return;
        if (!Array.isArray(picked) && !picked.uri)
          throw new Error("Picker returned no local file URI.");
        if (Array.isArray(picked) && picked.some((file) => !file.uri))
          throw new Error("Picker returned no local file URI.");
        const next = [
          ...attachmentsRef.current,
          ...(Array.isArray(picked) ? picked : [picked]),
        ];
        attachmentsRef.current = next;
        onAttachmentsChange?.(next);
      } catch {
        if (mounted.current && generation === operationGeneration.current)
          setError("Could not add the selected attachment. Please try again.");
      } finally {
        pickerInFlight.current = false;
      }
    },
    [disabled, onAttachmentsChange],
  );

  const send = useCallback(async () => {
    // Stop is an independent control, not a variant of sending. It must work
    // even when the draft is empty or the host has temporarily disabled input.
    if (busy) {
      if (!onStop || stopInFlight.current) return;
      const generation = operationGeneration.current;
      stopInFlight.current = true;
      setStopping(true);
      setError(null);
      try {
        await onStop();
      } catch {
        if (mounted.current && generation === operationGeneration.current)
          setError("Could not stop the current run. Please try again.");
      } finally {
        stopInFlight.current = false;
        if (mounted.current && generation === operationGeneration.current)
          setStopping(false);
      }
      return;
    }
    if (sendingRef.current) return;
    const originalDraft = draftRef.current;
    const text = originalDraft.trim();
    const currentAttachments = [...attachmentsRef.current];
    if (
      disabled ||
      pickerInFlight.current ||
      (!text && currentAttachments.length === 0)
    )
      return;
    const generation = operationGeneration.current;
    nativeSelectionRef.current = false;
    nativeEditTextRef.current = null;
    const originalSelection = selectionRef.current;
    dismissKeyboard({ revealLatest: true });
    sendingRef.current = true;
    setSending(true);
    setError(null);
    // Clear at once, as messengers do; a failed send restores the draft.
    const cleared = { start: 0, end: 0 };
    draftRef.current = "";
    selectionRef.current = cleared;
    setQuery(null);
    setSearched(NO_CANDIDATES);
    cancelSearch();
    onDraftChange("", cleared);
    try {
      await onSend({
        text,
        skillIds: [...selectedSkillIds],
        attachments: currentAttachments,
      });
    } catch {
      if (
        mounted.current &&
        generation === operationGeneration.current &&
        (draftRef.current === "" || draftRef.current === originalDraft)
      ) {
        const restoredSelection = {
          start: Math.min(originalSelection.start, originalDraft.length),
          end: Math.min(originalSelection.end, originalDraft.length),
        };
        nativeSelectionRef.current = false;
        nativeEditTextRef.current = null;
        draftRef.current = originalDraft;
        selectionRef.current = restoredSelection;
        onDraftChange(originalDraft, restoredSelection);
        setError(
          "Your message could not be sent. Your draft is still here. Please try again.",
        );
      }
    } finally {
      if (generation === operationGeneration.current)
        sendingRef.current = false;
      if (mounted.current && generation === operationGeneration.current)
        setSending(false);
    }
  }, [
    busy,
    cancelSearch,
    disabled,
    dismissKeyboard,
    onDraftChange,
    onSend,
    onStop,
    selectedSkillIds,
  ]);

  const hasPicker = Boolean(onTakePhoto || onChoosePhotos || onChooseFiles);
  const canSubmit =
    !disabled && (draft.trim().length > 0 || attachments.length > 0);
  const attachmentButton = hasPicker ? (
    <NativeMenu
      label="Add attachment"
      testID="chat-composer-attachments"
      actions={[
        ...(onTakePhoto
          ? [
              {
                id: "camera",
                title: "Take a photo",
                symbol: "camera",
                disabled,
              },
            ]
          : []),
        ...(onChoosePhotos
          ? [
              {
                id: "photos",
                title: "Choose photos",
                symbol: "photo.on.rectangle",
                disabled,
              },
            ]
          : []),
        ...(onChooseFiles
          ? [{ id: "files", title: "Choose files", symbol: "doc", disabled }]
          : []),
      ]}
      onSelect={(id) => {
        void addAttachments(
          id === "camera"
            ? onTakePhoto
            : id === "photos"
              ? onChoosePhotos
              : onChooseFiles,
        );
      }}
    >
      <View
        pointerEvents="none"
        style={[
          styles.control,
          styles.round,
          { backgroundColor: theme.controlFill, opacity: disabled ? 0.38 : 1 },
        ]}
      >
        <Plus size={22} color={theme.text} />
      </View>
    </NativeMenu>
  ) : null;
  const settingsButton =
    settings && onSettings ? (
      <NativeAction
        fitContent
        accessibilityRole="button"
        accessibilityLabel={`Chat settings: ${[
          settings.harness,
          settings.mode,
          settings.model,
          settings.effort,
        ]
          .filter(Boolean)
          .join(", ")}`}
        accessibilityHint="Choose model, effort and agent"
        onPress={onSettings}
        testID="chat-composer-settings"
        style={[
          styles.control,
          styles.pill,
          { backgroundColor: theme.controlFill },
        ]}
      >
        <Text
          numberOfLines={1}
          // Native hosts size the pill to its label; bound it beside +/send.
          style={[
            styles.pillText,
            { maxWidth: Math.max(96, windowWidth - 176) },
          ]}
        >
          {settings.mode === "plan" ? (
            <Text style={{ color: theme.mutedText }}>Plan · </Text>
          ) : null}
          <Text style={{ color: theme.text }}>
            {settings.model || "Choose model"}
          </Text>
          {settings.effort ? (
            <Text style={{ color: theme.mutedText }}> {settings.effort}</Text>
          ) : null}
        </Text>
      </NativeAction>
    ) : null;
  const sendButton = (
    <IconButton
      symbol={busy ? "stop.fill" : "arrow.up"}
      tone="primary"
      icon={
        busy ? (
          <Square
            size={16}
            color={theme.primaryForeground}
            fill={theme.primaryForeground}
          />
        ) : (
          <ArrowUp size={24} color={theme.primaryForeground} />
        )
      }
      label={busy ? "Stop OpenCuria" : "Send message"}
      disabled={busy ? !onStop || stopping : !canSubmit || sending}
      onPress={() => {
        void send();
      }}
      testID={busy ? "chat-composer-stop" : "chat-composer-send"}
    />
  );

  return (
    <View testID={testID} pointerEvents="box-none" style={styles.root}>
      {error ? (
        <Banner
          tone="error"
          message={error}
          onDismiss={() => setError(null)}
          testID="chat-composer-error"
          style={{ marginBottom: 8 }}
        />
      ) : null}
      {query && candidates.length > 0 ? (
        <View
          accessibilityLabel={`${query.kind === "skill" ? "Skills" : "Context"} suggestions`}
          style={[
            styles.suggestionPanel,
            { backgroundColor: theme.raised, borderColor: theme.border },
          ]}
        >
          <View style={styles.suggestionTitle}>
            <Text
              style={{
                color: theme.mutedText,
                fontSize: 12,
                fontWeight: "600",
              }}
            >
              {query.kind === "skill" ? "Skills" : "Add context"}
            </Text>
            <Text style={{ color: theme.subtleText, fontSize: 12 }}>
              Select to add
            </Text>
          </View>
          <ScrollView
            keyboardShouldPersistTaps="handled"
            scrollsToTop={false}
            style={{ maxHeight: 192 }}
          >
            {candidates.slice(0, 10).map((candidate) => (
              <NativeAction
                key={`${candidate.kind}:${candidate.insert}`}
                accessibilityRole="button"
                accessibilityLabel={`Add ${candidate.label}`}
                onPress={() => selectCandidate(candidate)}
                style={({ pressed }) => [
                  styles.suggestionItem,
                  {
                    backgroundColor: pressed
                      ? theme.surfaceMuted
                      : "transparent",
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
      ) : null}

      <ComposerDismissArea enabled={!inputScrolls}>
        <View
          collapsable={false}
          onLayout={onComposerLayout}
          style={styles.core}
        >
          <ComposerSurface>
            {activeSkills.length ? (
              <ScrollView
                horizontal
                scrollsToTop={false}
                showsHorizontalScrollIndicator={false}
                contentContainerStyle={styles.chips}
              >
                {activeSkills.map((skill) => (
                  <View
                    key={skill.id}
                    style={[
                      styles.skillChip,
                      { backgroundColor: theme.surfaceMuted },
                    ]}
                  >
                    <Sparkles size={13} color={theme.primary} />
                    <Text
                      numberOfLines={1}
                      style={[styles.skillName, { color: theme.text }]}
                    >
                      {skill.name}
                    </Text>
                    <NativeAction
                      accessibilityRole="button"
                      accessibilityLabel={`Remove ${skill.name} skill`}
                      hitSlop={6}
                      onPress={() =>
                        onSelectedSkillIdsChange?.(
                          selectedSkillIds.filter((id) => id !== skill.id),
                        )
                      }
                      style={styles.chipRemove}
                    >
                      <X size={14} color={theme.mutedText} />
                    </NativeAction>
                  </View>
                ))}
              </ScrollView>
            ) : null}
            {attachments.length ? (
              <ScrollView
                horizontal
                scrollsToTop={false}
                showsHorizontalScrollIndicator={false}
                contentContainerStyle={styles.chips}
              >
                {attachments.map((attachment) => (
                  <View
                    key={attachment.id}
                    style={[
                      styles.attachmentChip,
                      { backgroundColor: theme.surfaceMuted },
                    ]}
                  >
                    {isImage(attachment) ? (
                      <Image
                        source={{ uri: attachment.uri }}
                        contentFit="cover"
                        style={styles.attachmentThumbnail}
                        accessibilityLabel={`${attachment.name} preview`}
                      />
                    ) : (
                      <FileText size={14} color={theme.mutedText} />
                    )}
                    <Text
                      numberOfLines={1}
                      style={[styles.skillName, { color: theme.text }]}
                    >
                      {attachment.name}
                    </Text>
                    <NativeAction
                      accessibilityRole="button"
                      accessibilityLabel={`Remove ${attachment.name}`}
                      onPress={() => {
                        const next = attachmentsRef.current.filter(
                          (item) => item.id !== attachment.id,
                        );
                        attachmentsRef.current = next;
                        onAttachmentsChange?.(next);
                      }}
                      style={styles.chipRemove}
                    >
                      <X size={14} color={theme.mutedText} />
                    </NativeAction>
                  </View>
                ))}
              </ScrollView>
            ) : null}
            <ComposerEditor
              key="message-input"
              ref={inputRef}
              value={draft}
              onChangeText={changeText}
              onSelectionChange={onSelectionChange}
              onContentSizeChange={({ nativeEvent }) => {
                const scrolls =
                  nativeEvent.contentSize.height > INPUT_MAX_HEIGHT;
                setInputScrolls((current) =>
                  current === scrolls ? current : scrolls,
                );
              }}
              nativeID={COMPOSER_INPUT_NATIVE_ID}
              multiline
              editable={!disabled}
              placeholder={placeholder}
              placeholderTextColor={theme.subtleText}
              accessibilityLabel="Chat message"
              testID="chat-composer-input"
              returnKeyType="default"
              blurOnSubmit={false}
              textAlignVertical="top"
              style={[styles.input, { color: theme.text }]}
            />
            <View style={styles.toolbar}>
              {attachmentButton}
              <View style={styles.settings}>{settingsButton}</View>
              {sendButton}
            </View>
          </ComposerSurface>
          {disabled && !busy ? (
            <Text style={[styles.disabledHint, { color: theme.subtleText }]}>
              Chat is not available right now.
            </Text>
          ) : null}
        </View>
      </ComposerDismissArea>
    </View>
  );
}

function estimateSelectionAfterEdit(
  previous: string,
  next: string,
  selection: { start: number; end: number },
): { start: number; end: number } {
  let prefix = 0;
  const sharedLimit = Math.min(previous.length, next.length);
  while (prefix < sharedLimit && previous[prefix] === next[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < sharedLimit - prefix &&
    previous[previous.length - 1 - suffix] === next[next.length - 1 - suffix]
  )
    suffix += 1;
  const changedStart = Math.min(selection.start, prefix);
  const insertedLength = Math.max(0, next.length - prefix - suffix);
  const cursor = Math.min(next.length, changedStart + insertedLength);
  return { start: cursor, end: cursor };
}

function isImage(attachment: ComposerAttachment): boolean {
  return (
    attachment.kind === "image" ||
    attachment.mimeType?.toLowerCase().startsWith("image/") === true
  );
}

/** The editor grows with its draft up to this height, then scrolls. */
const INPUT_MAX_HEIGHT = 170;

const styles = StyleSheet.create({
  root: { paddingHorizontal: 12 },
  core: { paddingTop: 6 },
  input: {
    minWidth: 0,
    minHeight: 36,
    maxHeight: INPUT_MAX_HEIGHT,
    paddingVertical: 6,
    marginHorizontal: 12,
    marginTop: 6,
    fontSize: 17,
    lineHeight: 24,
  },
  toolbar: {
    minHeight: 48,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingLeft: 4,
    paddingTop: 2,
  },
  control: { height: 40, alignItems: "center", justifyContent: "center" },
  round: { width: 40, borderRadius: 20 },
  settings: { flex: 1, flexDirection: "row", minWidth: 0 },
  pill: { maxWidth: "100%", borderRadius: 20, paddingHorizontal: 15 },
  pillText: { fontSize: 15, fontWeight: "500" },
  chips: {
    alignItems: "center",
    gap: 7,
    paddingVertical: 8,
    paddingHorizontal: 12,
  },
  skillChip: {
    minHeight: 34,
    maxWidth: 240,
    borderRadius: 18,
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingLeft: 10,
    paddingRight: 4,
  },
  attachmentChip: {
    minHeight: 38,
    maxWidth: 260,
    borderRadius: 19,
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingLeft: 5,
    paddingRight: 4,
  },
  attachmentThumbnail: {
    width: 30,
    height: 30,
    borderRadius: 10,
    backgroundColor: "#777777",
  },
  skillName: { maxWidth: 170, fontSize: 13, fontWeight: "500" },
  chipRemove: {
    width: 30,
    height: 30,
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 15,
  },
  suggestionPanel: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 17,
    marginBottom: 8,
    overflow: "hidden",
  },
  suggestionTitle: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingHorizontal: 14,
    paddingTop: 12,
    paddingBottom: 6,
  },
  suggestionItem: {
    minHeight: 46,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingHorizontal: 14,
  },
  disabledHint: { fontSize: 12, textAlign: "center", paddingTop: 6 },
});
