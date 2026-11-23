import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Image } from "expo-image";
import {
  ArrowUp,
  FileText,
  Mic,
  Pencil,
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
import { buildComposerSheets } from "@opencuria/chat-core/composerSheets";
import type { HarnessScheduledMessage } from "@opencuria/chat-core/harness";
import { sendLaterOptions } from "@opencuria/chat-core/sendLater";
import { appendDictation } from "@opencuria/chat-core/voiceInput";
import { Banner, IconButton, NativeAction } from "../ui/primitives";
import { NativeContextMenu } from "../ui/ContextMenu";
import { NativeMenu, type NativeMenuAction } from "../ui/NativeMenu";
import { PickerPill } from "../ui/PickerPill";
import { ComposerSurface } from "../ui/ComposerSurface";
import { ComposerAction, ComposerActionContent } from "../ui/ComposerAction";
import { ComposerEditor } from "./ComposerEditor";
import { armPromptFlight } from "./PromptFlight";
import {
  applyDisplayEdit,
  composerDisplay,
  displayOffset,
  draftSelection,
  type ComposerDisplay,
} from "./composerDisplay";
import {
  ComposerSheetStack,
  type ComposerGates,
  type ComposerNotice,
  type ComposerQueue,
} from "./ComposerSheets";
import { SendLaterSheet } from "./SendLaterSheet";
import {
  VoiceClipRows,
  VoiceRecordingBar,
  type ComposerVoice,
} from "./ComposerVoice";
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
/** The workspace a new chat's prompt goes to, and the menu to change it. */
export interface ComposerWorkspace {
  name: string;
  /** One action per workspace; `selected` marks the current one. */
  actions: NativeMenuAction[];
  onSelect: (id: string) => void;
}

/** A sent prompt the field holds for editing. */
export interface ComposerEditing {
  /** The edited message; a new id focuses the field again. */
  id: string;
  onCancel: () => void;
}

/** What the composer hands to its host when sending or scheduling. */
export interface ComposerSubmission {
  text: string;
  skillIds: string[];
  attachments: ComposerAttachment[];
}

export interface ChatComposerProps {
  draft: string;
  onDraftChange: (
    draft: string,
    selection: { start: number; end: number },
  ) => void;
  onSend: (value: ComposerSubmission) => void | Promise<void>;
  /**
   * Schedules the draft like `onSend` sends it. Enables "Send later" on a
   * long press of the send button.
   */
  onSchedule?: (
    value: ComposerSubmission,
    sendAt: Date,
  ) => void | Promise<void>;
  onStop?: () => void | Promise<void>;
  /** A run or a send is in progress: Stop is available. */
  busy?: boolean;
  /**
   * The agent is working: a message steers the running turn, so the composer
   * offers Send next to a compact Stop while there is a draft.
   */
  steering?: boolean;
  disabled?: boolean;
  /**
   * False keeps the draft editable but neither sends it nor adds attachments,
   * e.g. offline.
   */
  canSend?: boolean;
  /** A new chat has no model yet: the draft stays, Send waits for one. */
  missingModel?: boolean;
  placeholder?: string;
  /** Workspace picker above the editor; hosts pass it only for new chats. */
  workspace?: ComposerWorkspace;
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
  /** Pending questions and permissions, shown as sheets above the editor. */
  gates?: ComposerGates;
  /** Queued steers and scheduled messages, shown as a sheet above the editor. */
  queue?: ComposerQueue;
  /** The chat's failed or stopped run, shown as a sheet above the editor. */
  notice?: ComposerNotice;
  /**
   * The draft is a sent prompt being edited: a banner above the field cancels
   * it, and `onSend` receives the edit.
   */
  editing?: ComposerEditing;
  /**
   * Dictation: a microphone takes the send button's place while the draft is
   * empty. Transcripts of this chat arrive at the end of the draft.
   */
  voice?: ComposerVoice;
  testID?: string;
}

/** Native symbols of the "Send later" presets. */
const SEND_LATER_SYMBOLS: Record<string, string> = {
  "in-1-hour": "clock",
  tomorrow: "sunrise",
  monday: "calendar",
};

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
  onSchedule,
  onStop,
  busy = false,
  steering = false,
  disabled = false,
  canSend = true,
  missingModel = false,
  placeholder = "Ask OpenCuria",
  workspace,
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
  gates,
  queue,
  notice,
  editing,
  voice,
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
  // The last native selection, in the coordinates of the shown text.
  const nativeShownSelectionRef = useRef({ start: 0, end: 0 });
  const draftRef = useRef(draft);
  const attachmentsRef = useRef(attachments);
  const pickerInFlight = useRef(false);
  const queryGeneration = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const operationGeneration = useRef(0);
  const mounted = useRef(true);
  const contextKeyRef = useRef(contextKey);
  // Text the mounted editor shows natively, and its mount generation.
  const editorTextRef = useRef(draft);
  const [editorKey, setEditorKey] = useState(0);
  // The draft or a scheduled message waiting for a custom send time.
  const [timePicker, setTimePicker] = useState<
    | { kind: "draft" }
    | { kind: "reschedule"; message: HarnessScheduledMessage }
    | null
  >(null);

  attachmentsRef.current = attachments;

  // The field shows file mentions by name; the draft keeps their tokens.
  const display = useMemo(() => composerDisplay(draft), [draft]);
  const displayCache = useRef(display);
  const displayOf = useCallback((text: string): ComposerDisplay => {
    if (displayCache.current.source !== text)
      displayCache.current = composerDisplay(text);
    return displayCache.current;
  }, []);

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

  const sheets = useMemo(
    () =>
      buildComposerSheets({
        mention:
          query && candidates.length
            ? { candidates: candidates.slice(0, 10), activeIndex: -1 }
            : null,
        questions: gates?.questions,
        permissions: gates?.permissions,
        queue: queue?.steers,
        scheduled: queue?.scheduled,
        notice: notice?.notice,
      }),
    [
      candidates,
      gates?.permissions,
      gates?.questions,
      notice?.notice,
      query,
      queue?.scheduled,
      queue?.steers,
    ],
  );

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
      // Skills apply when a turn starts; a steer cannot carry them.
      const slash = steering ? null : detectSlashQuery(text, cursor);
      if (slash !== null) {
        setSearched(NO_CANDIDATES);
        setQuery({ kind: "skill", query: slash });
      } else {
        setQuery(null);
        setSearched(NO_CANDIDATES);
      }
    },
    [cancelSearch, onSearchMentions, steering],
  );

  const changeText = useCallback(
    (shown: string) => {
      const previous = displayOf(draftRef.current);
      const edit = applyDisplayEdit(
        draftRef.current,
        shown,
        displayOffset(previous, selectionRef.current.start),
      );
      const next = displayOf(edit.draft);
      // Android reports the caret before the text, so it already belongs to
      // this edit; otherwise the caret follows the edited range.
      const nativeSelectionIsForThisEdit =
        nativeSelectionRef.current &&
        nativeEditTextRef.current === draftRef.current &&
        next.text === shown;
      const native = nativeShownSelectionRef.current;
      const nextSelection = nativeSelectionIsForThisEdit
        ? draftSelection(next, native)
        : { start: edit.cursor, end: edit.cursor };
      nativeSelectionRef.current = false;
      nativeEditTextRef.current = null;
      draftRef.current = edit.draft;
      editorTextRef.current = edit.draft;
      selectionRef.current = nextSelection;
      setError(null);
      onDraftChange(edit.draft, nextSelection);
      refreshQuery(edit.draft, nextSelection.end);
      if (next.text !== shown) {
        // A mention went away whole or got a separator: the field shows the
        // draft again, with the caret where the edit left it.
        const caret = displayOffset(next, edit.cursor);
        requestAnimationFrame(() =>
          inputRef.current?.setSelection(caret, caret),
        );
      }
    },
    [displayOf, onDraftChange, refreshQuery],
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
      // Keeps the focused editor; its next native edit corrects the height.
      editorTextRef.current = result.text;
      const nextSelection = { start: result.cursor, end: result.cursor };
      nativeSelectionRef.current = false;
      nativeEditTextRef.current = null;
      selectionRef.current = nextSelection;
      setQuery(null);
      setSearched(NO_CANDIDATES);
      setError(null);
      cancelSearch();
      onDraftChange(result.text, nextSelection);
      const caret = displayOffset(displayOf(result.text), result.cursor);
      // A command, not `setNativeProps`: Fabric would keep re-applying a
      // selection prop on every later render and pull the caret back.
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        inputRef.current?.setSelection(caret, caret);
      });
    },
    [
      cancelSearch,
      displayOf,
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
    setTimePicker(null);
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

  // Fabric sizes a multiline TextInput from the text native last reported, so
  // text replaced from JavaScript keeps the old height (react-native#59744):
  // a long draft cleared on send would leave a tall, empty composer. A fresh
  // editor measures the new draft; sending closes the keyboard anyway.
  useLayoutEffect(() => {
    if (editorTextRef.current === draft) return;
    editorTextRef.current = draft;
    setInputScrolls(false);
    setEditorKey((key) => key + 1);
  }, [draft]);

  // A prompt taken in for editing opens the keyboard, the caret at its end.
  // The frame lets the editor that measures the new draft mount first.
  const editingId = editing?.id;
  useEffect(() => {
    if (!editingId) return;
    const frame = requestAnimationFrame(() => {
      const input = inputRef.current;
      if (!input) return;
      const end = displayOf(draftRef.current).text.length;
      input.focus();
      input.setSelection(end, end);
    });
    return () => cancelAnimationFrame(frame);
  }, [displayOf, editingId]);

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
      const shown = event.nativeEvent.selection;
      nativeSelectionRef.current = true;
      nativeEditTextRef.current = draftRef.current;
      nativeShownSelectionRef.current = shown;
      const next = draftSelection(displayOf(draftRef.current), shown);
      selectionRef.current = next;
      refreshQuery(draftRef.current, next.end);
    },
    [displayOf, refreshQuery],
  );

  const addAttachments = useCallback(
    async (
      picker?:
        | ChatComposerProps["onTakePhoto"]
        | ChatComposerProps["onChoosePhotos"]
        | ChatComposerProps["onChooseFiles"],
    ) => {
      if (!picker || disabled || !canSend || pickerInFlight.current) return;
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
    [canSend, disabled, onAttachmentsChange],
  );

  // Stop is an independent control, not a variant of sending. It must work
  // even when the draft is empty or the host has temporarily disabled input.
  const stop = useCallback(async () => {
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
  }, [onStop]);

  /** Hands the draft to `deliver`; a failure gives the draft back. */
  const submit = useCallback(
    async (
      deliver: (value: ComposerSubmission) => void | Promise<void>,
      failure: string,
    ) => {
      if (sendingRef.current) return;
      const originalDraft = draftRef.current;
      const text = originalDraft.trim();
      const currentAttachments = [...attachmentsRef.current];
      if (
        disabled ||
        !canSend ||
        missingModel ||
        (busy && !steering) ||
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
      // Attachments leave with the text; a failed send gives both back.
      if (currentAttachments.length) {
        attachmentsRef.current = [];
        onAttachmentsChange?.([]);
      }
      try {
        await deliver({
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
          // Keep anything picked meanwhile.
          const missing = currentAttachments.filter(
            (file) =>
              !attachmentsRef.current.some((item) => item.id === file.id),
          );
          if (missing.length) {
            const next = [...missing, ...attachmentsRef.current];
            attachmentsRef.current = next;
            onAttachmentsChange?.(next);
          }
          setError(failure);
        }
      } finally {
        if (generation === operationGeneration.current)
          sendingRef.current = false;
        if (mounted.current && generation === operationGeneration.current)
          setSending(false);
      }
    },
    [
      busy,
      canSend,
      missingModel,
      cancelSearch,
      disabled,
      dismissKeyboard,
      onAttachmentsChange,
      onDraftChange,
      selectedSkillIds,
      steering,
    ],
  );

  const isEditing = Boolean(editing);
  const send = useCallback(() => {
    // A prompt's bubble rises from where its text is now; a steer goes to the
    // queue above the composer instead, and an edit replaces its prompt.
    if (!steering && !isEditing)
      armPromptFlight((report) => {
        const input = inputRef.current;
        if (!input) return report(null);
        input.measureInWindow((_x, y) => report(y));
      });
    return submit(
      onSend,
      "Your message could not be sent. Your draft is still here. Please try again.",
    );
  }, [isEditing, onSend, steering, submit]);

  // Dictation arrives at the end of the draft; "Stop and send" sends it.
  const sendRef = useRef(send);
  sendRef.current = send;
  const onDraftChangeRef = useRef(onDraftChange);
  onDraftChangeRef.current = onDraftChange;
  const receiveVoice = voice?.receive;
  useEffect(() => {
    if (!receiveVoice) return;
    return receiveVoice((text, sendNow) => {
      const next = appendDictation(draftRef.current, text);
      const end = { start: next.length, end: next.length };
      nativeSelectionRef.current = false;
      nativeEditTextRef.current = null;
      draftRef.current = next;
      selectionRef.current = end;
      setError(null);
      onDraftChangeRef.current(next, end);
      // After the draft reached the host and the field shows it.
      if (sendNow)
        requestAnimationFrame(() => {
          if (mounted.current) void sendRef.current();
        });
    });
  }, [receiveVoice]);

  const schedule = useCallback(
    (sendAt: Date) => {
      if (!onSchedule) return;
      void submit(
        (value) => onSchedule(value, sendAt),
        "Your message could not be scheduled. Your draft is still here. Please try again.",
      );
    },
    [onSchedule, submit],
  );

  const chooseSendLater = useCallback(
    (id: string) => {
      if (id === "custom") {
        dismissKeyboard();
        setTimePicker({ kind: "draft" });
        return;
      }
      // Recomputed on selection, so "In 1 hour" counts from now.
      const option = sendLaterOptions().find((item) => item.id === id);
      if (option) schedule(option.at);
    },
    [dismissKeyboard, schedule],
  );

  const hasPicker = Boolean(onTakePhoto || onChoosePhotos || onChooseFiles);
  const hasDraft = draft.trim().length > 0 || attachments.length > 0;
  // While the agent works a draft steers it; a send still on its way blocks.
  const canSubmit =
    !disabled && canSend && !missingModel && hasDraft && (!busy || steering);
  const primaryIsStop = busy && !(steering && hasDraft);
  const attachDisabled = disabled || !canSend;
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
                disabled: attachDisabled,
              },
            ]
          : []),
        ...(onChoosePhotos
          ? [
              {
                id: "photos",
                title: "Choose photos",
                symbol: "photo.on.rectangle",
                disabled: attachDisabled,
              },
            ]
          : []),
        ...(onChooseFiles
          ? [
              {
                id: "files",
                title: "Choose files",
                symbol: "doc",
                disabled: attachDisabled,
              },
            ]
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
      <View pointerEvents="none">
        <ComposerActionContent icon={Plus} disabled={attachDisabled} />
      </View>
    </NativeMenu>
  ) : null;
  const settingsButton =
    settings && onSettings ? (
      <PickerPill
        value={settings.model || "Choose model"}
        detail={settings.effort || undefined}
        prefix={settings.mode === "plan" ? "Plan · " : undefined}
        // Bound the native label beside the attachment and send targets.
        maxWidth={Math.max(64, windowWidth - 186)}
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
      />
    ) : null;
  const workspacePicker = workspace ? (
    <View style={styles.workspace}>
      <PickerPill
        value={workspace.name}
        maxWidth={Math.max(64, windowWidth - 88)}
        accessibilityLabel={`Workspace: ${workspace.name}`}
        menu={{
          title: "Workspace",
          // A prompt on its way stays with the workspace it was sent to.
          actions: sending
            ? workspace.actions.map((action) => ({ ...action, disabled: true }))
            : workspace.actions,
          onSelect: workspace.onSelect,
        }}
        testID="chat-composer-workspace"
      />
    </View>
  ) : null;
  const stopDisabled = !onStop || stopping;
  const stopButton = (
    <ComposerAction
      label="Stop OpenCuria"
      icon={Square}
      primary={primaryIsStop}
      iconSize={primaryIsStop ? 14 : 13}
      filledIcon
      busy={!primaryIsStop && stopping}
      disabled={stopDisabled}
      onPress={() => {
        void stop();
      }}
      testID="chat-composer-stop"
    />
  );
  const sendDisabled = !canSubmit || sending;
  const canSchedule = Boolean(onSchedule) && !sendDisabled;
  const sendAction = (showMenu?: () => void) => (
    <ComposerAction
      label="Send message"
      icon={ArrowUp}
      primary
      iconSize={22}
      disabled={sendDisabled}
      onPress={() => {
        void send();
      }}
      onLongPress={showMenu}
      accessibilityHint={
        missingModel
          ? "Choose a model first"
          : canSchedule
            ? "Hold to send later"
            : undefined
      }
      testID="chat-composer-send"
    />
  );
  const canDictate = Boolean(voice) && !disabled;
  const voiceButton = (primary: boolean) => (
    <ComposerAction
      label="Dictate a message"
      icon={Mic}
      primary={primary}
      iconSize={primary ? 20 : 19}
      onPress={() => voice?.start()}
      testID="chat-composer-voice"
    />
  );
  const sendButton = primaryIsStop ? (
    stopButton
  ) : canDictate && !hasDraft ? (
    voiceButton(true)
  ) : canSchedule ? (
    <NativeContextMenu
      label="Send later"
      fitContent
      actions={[
        ...sendLaterOptions().map((option) => ({
          id: option.id,
          title:
            option.id === "in-1-hour"
              ? option.label
              : `${option.label}, ${option.detail}`,
          symbol: SEND_LATER_SYMBOLS[option.id],
        })),
        {
          id: "custom",
          title: "Choose date & time…",
          symbol: "calendar.badge.clock",
        },
      ]}
      onSelect={chooseSendLater}
    >
      {sendAction}
    </NativeContextMenu>
  ) : (
    sendAction()
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
      {voice?.notice ? (
        <Banner
          tone={voice.notice.tone === "error" ? "error" : "neutral"}
          message={voice.notice.message}
          actionLabel={voice.notice.action?.label}
          onAction={voice.notice.action?.onPress}
          onDismiss={voice.dismissNotice}
          testID="chat-composer-voice-notice"
          style={{ marginBottom: 8 }}
        />
      ) : null}
      <ComposerSheetStack
        sheets={sheets}
        gates={gates}
        queue={queue}
        onDismissNotice={notice?.onDismiss}
        onChangeTime={(message) => {
          dismissKeyboard();
          setTimePicker({ kind: "reschedule", message });
        }}
        onSelectCandidate={selectCandidate}
      />
      <ComposerDismissArea enabled={!inputScrolls}>
        <View
          collapsable={false}
          onLayout={onComposerLayout}
          style={styles.core}
        >
          <ComposerSurface>
            {editing ? (
              <View
                testID="chat-composer-editing"
                style={[styles.editing, { backgroundColor: theme.controlFill }]}
              >
                <Pencil size={18} strokeWidth={1.8} color={theme.mutedText} />
                <Text
                  numberOfLines={1}
                  style={[styles.editingLabel, { color: theme.mutedText }]}
                >
                  Editing message
                </Text>
                <IconButton
                  symbol="xmark"
                  icon={<X size={18} color={theme.mutedText} />}
                  label="Cancel editing"
                  tone="quiet"
                  onPress={editing.onCancel}
                  testID="chat-composer-cancel-edit"
                />
              </View>
            ) : null}
            {workspacePicker}
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
                        style={[
                          styles.attachmentThumbnail,
                          { backgroundColor: theme.controlFill },
                        ]}
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
            {voice?.clips.length ? (
              <VoiceClipRows
                clips={voice.clips}
                onRetry={voice.retry}
                onDiscard={voice.discard}
              />
            ) : null}
            <ComposerEditor
              key={`message-input-${editorKey}`}
              ref={inputRef}
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
            >
              {display.pieces.map((piece, index) =>
                piece.segment.kind === "text" ? (
                  piece.text
                ) : (
                  <Text
                    key={`mention:${index}`}
                    style={[
                      styles.mention,
                      {
                        color: theme.primary,
                        backgroundColor: mentionTint(theme.primary),
                      },
                    ]}
                  >
                    {piece.text}
                  </Text>
                ),
              )}
            </ComposerEditor>
            {voice?.recording ? (
              <VoiceRecordingBar
                recording={voice.recording}
                onCancel={voice.cancel}
                onFinish={voice.finish}
              />
            ) : (
              <View style={styles.toolbar}>
                {attachmentButton}
                <View style={styles.settings}>{settingsButton}</View>
                {busy && !primaryIsStop ? stopButton : null}
                {/* While the agent works, a steer can be dictated beside Stop. */}
                {primaryIsStop && steering && canDictate
                  ? voiceButton(false)
                  : null}
                {sendButton}
              </View>
            )}
          </ComposerSurface>
        </View>
      </ComposerDismissArea>
      <SendLaterSheet
        visible={timePicker !== null}
        initial={
          timePicker?.kind === "reschedule"
            ? new Date(timePicker.message.send_at)
            : null
        }
        actionTitle={
          timePicker?.kind === "reschedule" ? "Reschedule" : "Schedule"
        }
        onClose={() => setTimePicker(null)}
        onConfirm={(sendAt) => {
          const picked = timePicker;
          setTimePicker(null);
          if (picked?.kind === "reschedule")
            void Promise.resolve(
              queue?.onReschedule(picked.message.id, sendAt),
            ).catch(() => undefined);
          else schedule(sendAt);
        }}
      />
    </View>
  );
}

/** The accent at low opacity: the background of a mention in the field. */
function mentionTint(color: string): string {
  const hex = /^#([0-9a-f]{6})$/i.exec(color)?.[1];
  if (!hex) return "transparent";
  const value = parseInt(hex, 16);
  return `rgba(${value >> 16}, ${(value >> 8) & 255}, ${value & 255}, 0.16)`;
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
  // The name lines up with the editor's text below it.
  workspace: { flexDirection: "row", marginHorizontal: 8, marginBottom: -6 },
  // Its corners follow the surface's, inset by the surface padding.
  editing: {
    minHeight: 44,
    borderRadius: 22,
    borderCurve: "continuous",
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingLeft: 14,
    marginBottom: 2,
  },
  editingLabel: { flex: 1, fontSize: 15 },
  input: {
    minWidth: 0,
    minHeight: 36,
    maxHeight: INPUT_MAX_HEIGHT,
    paddingHorizontal: 0,
    paddingVertical: 6,
    marginHorizontal: 12,
    marginTop: 6,
    fontSize: 17,
    lineHeight: 24,
  },
  toolbar: {
    minHeight: 44,
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
  },
  settings: { flex: 1, flexDirection: "row", minWidth: 0 },
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
  },
  skillName: { maxWidth: 170, fontSize: 13, fontWeight: "500" },
  mention: { fontWeight: "600" },
  chipRemove: {
    width: 30,
    height: 30,
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 15,
  },
});
