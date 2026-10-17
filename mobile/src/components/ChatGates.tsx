import React, { useEffect, useState } from "react";
import {
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  ShieldCheck,
} from "lucide-react-native";
import type {
  HarnessPermissionRequest,
  HarnessPermissionResponse,
  HarnessQuestionRequest,
} from "@opencuria/chat-core/harness";
import { Banner, Button, Sheet } from "../ui/primitives";
import { useTheme } from "../ui/theme";

export interface PermissionGateProps {
  requests: HarnessPermissionRequest[];
  resolving?: boolean;
  onResolve: (
    requestId: string,
    response: HarnessPermissionResponse,
  ) => void | Promise<void>;
  onClose?: () => void;
  visible?: boolean;
  testID?: string;
}

/** Native approval UI; every decision is delegated to the parent controller. */
export function PermissionGate({
  requests,
  resolving = false,
  onResolve,
  onClose = () => undefined,
  visible = requests.length > 0,
  testID = "chat-permission-gate",
}: PermissionGateProps): React.JSX.Element {
  const { theme } = useTheme();
  const [page, setPage] = useState(0);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setPage(0);
    setError(null);
  }, [requests[0]?.request_id]);
  const request = requests[Math.min(page, Math.max(requests.length - 1, 0))];
  const resolve = async (response: HarnessPermissionResponse) => {
    if (!request || resolving) return;
    setError(null);
    try {
      await onResolve(request.request_id, response);
    } catch {
      setError("Could not update this permission. Please try again.");
    }
  };
  return (
    <Sheet
      visible={visible && Boolean(request)}
      onClose={onClose}
      title="Permission required"
      testID={testID}
      dismissible={false}
      maxHeight="86%"
    >
      {request ? (
        <>
          <View style={styles.gateIntro}>
            <View
              style={[styles.gateIcon, { backgroundColor: theme.surfaceMuted }]}
            >
              <ShieldCheck size={21} color={theme.warning} />
            </View>
            <Text style={[styles.gateCopy, { color: theme.mutedText }]}>
              {request.agent_name
                ? `The ${request.agent_name} agent`
                : "OpenCuria"}{" "}
              wants to use a tool. Choose whether this action is allowed.
            </Text>
          </View>
          {requests.length > 1 ? (
            <Pager
              page={page}
              total={requests.length}
              onPrevious={() => setPage(Math.max(0, page - 1))}
              onNext={() => setPage(Math.min(requests.length - 1, page + 1))}
              label="Permission"
              disabled={resolving}
            />
          ) : null}
          <View style={styles.detailSection}>
            <Text style={[styles.detailLabel, { color: theme.subtleText }]}>
              TOOL
            </Text>
            <Text style={[styles.detailValue, { color: theme.text }]}>
              {request.tool}
            </Text>
          </View>
          <View style={styles.detailSection}>
            <Text style={[styles.detailLabel, { color: theme.subtleText }]}>
              ACTION
            </Text>
            <Text style={[styles.detailValue, { color: theme.text }]}>
              {request.title}
            </Text>
          </View>
          {request.pattern ? (
            <View
              style={[
                styles.preformatted,
                { backgroundColor: theme.codeBackground },
              ]}
            >
              <Text
                selectable
                style={[styles.preformattedText, { color: theme.text }]}
              >
                {request.pattern}
              </Text>
            </View>
          ) : null}
          {error ? (
            <Banner
              tone="error"
              message={error}
              testID="chat-permission-error"
            />
          ) : null}
          <View style={styles.gateButtons}>
            <Button
              title="Reject"
              variant="outline"
              onPress={() => {
                void resolve("reject");
              }}
              busy={resolving}
              style={{ flex: 1 }}
            />
            <Button
              title="Allow once"
              variant="secondary"
              onPress={() => {
                void resolve("once");
              }}
              busy={resolving}
              style={{ flex: 1 }}
            />
          </View>
          <Button
            title="Always allow this tool"
            variant="primary"
            onPress={() => {
              void resolve("always");
            }}
            busy={resolving}
            style={{ alignSelf: "stretch" }}
          />
        </>
      ) : null}
    </Sheet>
  );
}

export interface QuestionGateProps {
  requests: HarnessQuestionRequest[];
  submitting?: boolean;
  onSubmit: (requestId: string, answers: string[]) => void | Promise<void>;
  onSkip: (requestId: string) => void | Promise<void>;
  onClose?: () => void;
  visible?: boolean;
}

export interface ChatGatesProps {
  permissions?: PermissionGateProps["requests"];
  questions?: QuestionGateProps["requests"];
  resolving?: boolean;
  submitting?: boolean;
  onResolvePermission: PermissionGateProps["onResolve"];
  onSubmitQuestion: QuestionGateProps["onSubmit"];
  onSkipQuestion: QuestionGateProps["onSkip"];
  /** Parent-selected next gate. Defaults to permission when both kinds are pending. */
  priority?: "permission" | "question";
  testID?: string;
}

/**
 * Single-Modal gate host. Permission and question requests can coexist during a
 * run; only the parent-selected next action mounts, avoiding competing native
 * Modals. Default ordering is permission first, then question.
 */
export function ChatGates({
  permissions = [],
  questions = [],
  resolving,
  submitting,
  onResolvePermission,
  onSubmitQuestion,
  onSkipQuestion,
  priority,
  testID = "chat-gates",
}: ChatGatesProps): React.JSX.Element {
  const gate = priority ?? (permissions.length ? "permission" : "question");
  if (gate === "question" && questions.length) {
    return (
      <QuestionGate
        requests={questions}
        submitting={submitting}
        onSubmit={onSubmitQuestion}
        onSkip={onSkipQuestion}
        visible
        testID={`${testID}-question`}
      />
    );
  }
  if (permissions.length) {
    return (
      <PermissionGate
        requests={permissions}
        resolving={resolving}
        onResolve={onResolvePermission}
        visible
        testID={`${testID}-permission`}
      />
    );
  }
  if (questions.length) {
    return (
      <QuestionGate
        requests={questions}
        submitting={submitting}
        onSubmit={onSubmitQuestion}
        onSkip={onSkipQuestion}
        visible
        testID={`${testID}-question`}
      />
    );
  }
  return <View testID={testID} />;
}

const MULTI_SELECT_SEPARATOR = "\0";

/** Native single/multi/free-text question UI preserving the protocol NUL-joined answer format. */
export function QuestionGate({
  requests,
  submitting = false,
  onSubmit,
  onSkip,
  onClose = () => undefined,
  visible = requests.length > 0,
  testID = "chat-question-gate",
}: QuestionGateProps & { testID?: string }): React.JSX.Element {
  const { theme } = useTheme();
  const [page, setPage] = useState(0);
  const [answers, setAnswers] = useState<Record<string, string[]>>({});
  const [custom, setCustom] = useState<Record<string, string[]>>({});
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setPage(0);
    setError(null);
  }, [requests[0]?.request_id]);
  const request = requests[Math.min(page, Math.max(requests.length - 1, 0))];
  const values = request
    ? (answers[request.request_id] ?? request.questions.map(() => ""))
    : [];
  const customValues = request
    ? (custom[request.request_id] ?? request.questions.map(() => ""))
    : [];

  const setAnswer = (index: number, value: string) => {
    if (!request) return;
    setAnswers((current) => {
      const next = [
        ...(current[request.request_id] ?? request.questions.map(() => "")),
      ];
      next[index] = value;
      return { ...current, [request.request_id]: next };
    });
  };
  const setCustomAnswer = (index: number, value: string) => {
    if (!request) return;
    setCustom((current) => {
      const next = [
        ...(current[request.request_id] ?? request.questions.map(() => "")),
      ];
      next[index] = value;
      return { ...current, [request.request_id]: next };
    });
    if (
      request.questions[index]?.options?.length &&
      !request.questions[index]?.multiple &&
      value.trim()
    )
      setAnswer(index, "");
  };
  const toggle = (questionIndex: number, label: string, multiple: boolean) => {
    const selected = (values[questionIndex] ?? "")
      .split(MULTI_SELECT_SEPARATOR)
      .filter(Boolean);
    if (multiple)
      setAnswer(
        questionIndex,
        selected.includes(label)
          ? selected
              .filter((item) => item !== label)
              .join(MULTI_SELECT_SEPARATOR)
          : [...selected, label].join(MULTI_SELECT_SEPARATOR),
      );
    else {
      setAnswer(questionIndex, label);
      setCustom((current) => {
        const next = [
          ...(current[request!.request_id] ?? request!.questions.map(() => "")),
        ];
        next[questionIndex] = "";
        return { ...current, [request!.request_id]: next };
      });
    }
  };

  const collected =
    request?.questions.map((question, index) => {
      const free = (customValues[index] ?? "").trim();
      if (!question.options?.length) return free;
      const selected = (values[index] ?? "")
        .split(MULTI_SELECT_SEPARATOR)
        .filter(Boolean);
      if (question.multiple)
        return [...selected, ...(free ? [free] : [])].join(
          MULTI_SELECT_SEPARATOR,
        );
      return free || selected[0] || "";
    }) ?? [];

  const submit = async () => {
    if (!request || submitting) return;
    setError(null);
    try {
      await onSubmit(request.request_id, collected);
    } catch {
      setError("Could not submit these answers. Please try again.");
    }
  };
  const skip = async () => {
    if (!request || submitting) return;
    setError(null);
    try {
      await onSkip(request.request_id);
    } catch {
      setError("Could not skip this question. Please try again.");
    }
  };

  return (
    <Sheet
      visible={visible && Boolean(request)}
      onClose={onClose}
      title="A few questions"
      testID={testID}
      dismissible={false}
      maxHeight="90%"
      footer={
        request ? (
          <View style={styles.gateButtons}>
            <Button
              title="Skip"
              variant="ghost"
              onPress={() => {
                void skip();
              }}
              disabled={submitting}
              style={{ flex: 1 }}
            />
            <Button
              title="Submit answers"
              variant="primary"
              onPress={() => {
                void submit();
              }}
              busy={submitting}
              style={{ flex: 1 }}
            />
          </View>
        ) : undefined
      }
    >
      {request ? (
        <>
          <View style={styles.gateIntro}>
            <View
              style={[styles.gateIcon, { backgroundColor: theme.surfaceMuted }]}
            >
              <CircleHelp size={21} color={theme.primary} />
            </View>
            <Text style={[styles.gateCopy, { color: theme.mutedText }]}>
              {request.agent_name
                ? `The ${request.agent_name} agent`
                : "OpenCuria"}{" "}
              needs your input to continue.
            </Text>
          </View>
          {requests.length > 1 ? (
            <Pager
              page={page}
              total={requests.length}
              onPrevious={() => setPage(Math.max(0, page - 1))}
              onNext={() => setPage(Math.min(requests.length - 1, page + 1))}
              label="Question"
              disabled={submitting}
            />
          ) : null}
          {error ? (
            <Banner tone="error" message={error} testID="chat-question-error" />
          ) : null}
          <ScrollView
            keyboardShouldPersistTaps="handled"
            contentContainerStyle={{ gap: 18 }}
          >
            {request.questions.map((question, index) => (
              <View
                key={`${request.request_id}-${index}`}
                style={styles.questionSection}
              >
                {question.header ? (
                  <Text
                    style={[styles.detailLabel, { color: theme.subtleText }]}
                  >
                    {question.header}
                  </Text>
                ) : null}
                <Text
                  accessibilityRole="header"
                  style={[styles.questionTitle, { color: theme.text }]}
                >
                  {question.question}
                </Text>
                {question.options?.map((option) => {
                  const selected = (values[index] ?? "")
                    .split(MULTI_SELECT_SEPARATOR)
                    .filter(Boolean)
                    .includes(option.label);
                  return (
                    <Pressable
                      key={option.label}
                      accessibilityRole={
                        question.multiple ? "checkbox" : "radio"
                      }
                      accessibilityState={{ checked: selected }}
                      accessibilityLabel={`${option.label}${option.description ? `, ${option.description}` : ""}`}
                      disabled={submitting}
                      onPress={() =>
                        toggle(index, option.label, Boolean(question.multiple))
                      }
                      style={[
                        styles.option,
                        {
                          backgroundColor: selected
                            ? theme.surfaceMuted
                            : theme.surface,
                          borderColor: selected ? theme.primary : theme.border,
                        },
                      ]}
                    >
                      <View
                        style={[
                          styles.optionIndicator,
                          {
                            borderColor: selected
                              ? theme.primary
                              : theme.subtleText,
                            backgroundColor: selected
                              ? theme.primary
                              : "transparent",
                          },
                        ]}
                      >
                        {selected ? (
                          <Check size={14} color={theme.primaryForeground} />
                        ) : null}
                      </View>
                      <View style={{ flex: 1, gap: 2 }}>
                        <Text
                          style={{
                            color: theme.text,
                            fontSize: 14,
                            lineHeight: 20,
                            fontWeight: "500",
                          }}
                        >
                          {option.label}
                        </Text>
                        {option.description ? (
                          <Text
                            style={{
                              color: theme.mutedText,
                              fontSize: 13,
                              lineHeight: 18,
                            }}
                          >
                            {option.description}
                          </Text>
                        ) : null}
                      </View>
                    </Pressable>
                  );
                })}
                <TextInput
                  multiline
                  value={customValues[index] ?? ""}
                  onChangeText={(value) => setCustomAnswer(index, value)}
                  editable={!submitting}
                  placeholder={
                    question.options?.length ? "Other answer…" : "Your answer…"
                  }
                  placeholderTextColor={theme.subtleText}
                  accessibilityLabel={`Your answer for ${question.question}`}
                  style={[
                    styles.freeInput,
                    {
                      color: theme.text,
                      backgroundColor: theme.surfaceMuted,
                      borderColor: theme.border,
                    },
                  ]}
                />
              </View>
            ))}
          </ScrollView>
        </>
      ) : null}
    </Sheet>
  );
}

function Pager({
  page,
  total,
  onPrevious,
  onNext,
  label,
  disabled = false,
}: {
  page: number;
  total: number;
  onPrevious: () => void;
  onNext: () => void;
  label: string;
  disabled?: boolean;
}): React.JSX.Element {
  const { theme } = useTheme();
  return (
    <View style={styles.pager}>
      <Text style={{ flex: 1, color: theme.mutedText, fontSize: 13 }}>
        {label} {page + 1} of {total}
      </Text>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Previous ${label.toLowerCase()}`}
        disabled={disabled || page === 0}
        onPress={onPrevious}
        style={styles.pagerButton}
      >
        <ChevronLeft
          size={19}
          color={disabled || page === 0 ? theme.subtleText : theme.text}
        />
      </Pressable>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Next ${label.toLowerCase()}`}
        disabled={disabled || page === total - 1}
        onPress={onNext}
        style={styles.pagerButton}
      >
        <ChevronRight
          size={19}
          color={disabled || page === total - 1 ? theme.subtleText : theme.text}
        />
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  gateIntro: { flexDirection: "row", alignItems: "center", gap: 12 },
  gateIcon: {
    width: 42,
    height: 42,
    borderRadius: 14,
    alignItems: "center",
    justifyContent: "center",
  },
  gateCopy: { flex: 1, fontSize: 14, lineHeight: 21 },
  pager: { minHeight: 42, flexDirection: "row", alignItems: "center", gap: 6 },
  pagerButton: {
    width: 40,
    height: 40,
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 20,
  },
  detailSection: { gap: 5 },
  detailLabel: {
    fontSize: 11,
    lineHeight: 15,
    letterSpacing: 0.7,
    fontWeight: "600",
    textTransform: "uppercase",
  },
  detailValue: { fontSize: 15, lineHeight: 22, fontWeight: "500" },
  preformatted: { maxHeight: 180, borderRadius: 13, padding: 12 },
  preformattedText: { fontFamily: "monospace", fontSize: 12, lineHeight: 18 },
  gateButtons: { flexDirection: "row", gap: 8 },
  questionSection: { gap: 9 },
  questionTitle: { fontSize: 16, lineHeight: 23, fontWeight: "600" },
  option: {
    minHeight: 54,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 14,
    paddingHorizontal: 12,
    paddingVertical: 9,
    flexDirection: "row",
    alignItems: "center",
    gap: 11,
  },
  optionIndicator: {
    width: 23,
    height: 23,
    borderRadius: 12,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  freeInput: {
    minHeight: 52,
    maxHeight: 130,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 13,
    padding: 12,
    fontSize: 14,
    lineHeight: 20,
    textAlignVertical: "top",
  },
});
