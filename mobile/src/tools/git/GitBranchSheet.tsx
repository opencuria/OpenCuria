import React, { useEffect, useState } from "react";
import { Text } from "react-native";
import { Button, NativeModal } from "../../ui/primitives";
import { NativeOption } from "../../ui/selection";
import { NativeTextField } from "../../ui/NativeTextField";
import { useTheme } from "../../ui/theme";

export type GitBranchForm =
  | { mode: "create"; fromHash: string; baseLabel: string; stash?: string }
  | { mode: "rename"; branch: string };

/** Create (at a commit or from a stash) or rename a branch, like the webapp dialog. */
export function GitBranchSheet({
  form,
  busy,
  onClose,
  onSubmit,
}: {
  form: GitBranchForm | null;
  busy: boolean;
  onClose: () => void;
  onSubmit: (name: string, checkout: boolean) => Promise<boolean>;
}): React.JSX.Element | null {
  const { theme } = useTheme();
  const [name, setName] = useState("");
  const [checkout, setCheckout] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => {
    if (!form) return;
    setName(form.mode === "rename" ? form.branch : "");
    setCheckout(true);
  }, [form]);
  if (!form) return null;
  const trimmed = name.trim();
  const canSubmit =
    !submitting &&
    !busy &&
    trimmed.length > 0 &&
    !(form.mode === "rename" && trimmed === form.branch);
  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      if (await onSubmit(trimmed, checkout)) onClose();
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <NativeModal
      visible
      onClose={onClose}
      title={form.mode === "create" ? "Create branch" : "Rename branch"}
      testID="git-branch-sheet"
    >
      <Text style={{ color: theme.mutedText, fontSize: 15 }}>
        {form.mode === "create"
          ? `New branch at ${form.baseLabel}${form.stash ? " (the stash is applied onto the new branch)" : ""}.`
          : `Rename ${form.branch}.`}
      </Text>
      <NativeTextField
        label="Branch name"
        value={name}
        onChangeText={setName}
        placeholder="e.g. feature/my-work"
        autoFocus
        returnKeyType="done"
        onSubmitEditing={() => void submit()}
        testID="git-branch-name"
      />
      {form.mode === "create" && !form.stash ? (
        <NativeOption
          label="Check out new branch"
          selected={checkout}
          multiple
          onPress={() => setCheckout((value) => !value)}
        />
      ) : null}
      <Button
        title={form.mode === "create" ? "Create branch" : "Rename"}
        variant="primary"
        busy={submitting}
        disabled={!canSubmit}
        onPress={() => void submit()}
        testID="git-branch-submit"
      />
    </NativeModal>
  );
}
