import React, { useEffect, useState } from "react";
import { Alert } from "react-native";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { SKILLS, canEditSkill } from "./SkillsPage";

/**
 * Creates or edits a skill. Organization skills are read-only for members;
 * sharing is chosen once, when an admin creates the skill.
 */
export function SkillPage({
  skillId,
}: {
  /** `null` creates a skill. */
  skillId: string | null;
}): React.JSX.Element {
  const { client, resources, isAdmin, refreshWorkspaces } = useSettings();
  const navigation = useSettingsNavigation();
  const skills = useResource(resources, SKILLS, () => client.skills.list());
  const skill =
    skillId === null
      ? null
      : ((skills.data ?? []).find((entry) => entry.id === skillId) ?? null);
  const [form, setForm] = useState<{
    name: string;
    body: string;
    shared: boolean;
  } | null>(skillId === null ? { name: "", body: "", shared: false } : null);
  useEffect(() => {
    if (skill && !form)
      setForm({ name: skill.name, body: skill.body, shared: false });
  }, [skill, form]);
  const [busy, setBusy] = useState<"save" | "delete" | null>(null);
  const editable = !skill || canEditSkill(skill, isAdmin);
  const valid = Boolean(form?.name.trim() && form.body.trim());

  const run = async (
    kind: "save" | "delete",
    action: () => Promise<void>,
    success: string,
    failure: string,
  ) => {
    setBusy(kind);
    try {
      await action();
      showToast({ title: success, tone: "success" });
      // The composer offers skills from the overview.
      void refreshWorkspaces();
      navigation.pop();
    } catch (cause) {
      showToast({
        title: failure,
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  };

  const save = () => {
    if (!form || !valid) return;
    const name = form.name.trim();
    const body = form.body.trim();
    void run(
      "save",
      async () => {
        if (skill) {
          const saved = await client.skills.update(skill.id, { name, body });
          skills.set((current) =>
            (current ?? []).map((entry) =>
              entry.id === saved.id ? saved : entry,
            ),
          );
        } else {
          const created = await client.skills.create({
            name,
            body,
            organization_skill: isAdmin && form.shared,
          });
          skills.set((current) => [created, ...(current ?? [])]);
        }
      },
      skill ? "Skill updated" : "Skill created",
      skill ? "Update failed" : "Creation failed",
    );
  };

  const remove = () =>
    Alert.alert(`Delete ${skill?.name ?? "skill"}?`, "This cannot be undone.", [
      { text: "Cancel", style: "cancel" },
      {
        text: "Delete",
        style: "destructive",
        onPress: () =>
          void run(
            "delete",
            async () => {
              await client.skills.delete(skill!.id);
              skills.set((current) =>
                (current ?? []).filter((entry) => entry.id !== skill!.id),
              );
            },
            "Skill deleted",
            "Deletion failed",
          ),
      },
    ]);

  const sections: NativeFormSection[] = form
    ? [
        {
          id: "skill",
          footer: editable
            ? "Markdown appended to harness prompts."
            : "Only organization admins can edit organization skills.",
          rows: [
            {
              kind: "text",
              id: "skill-name",
              label: "Name",
              placeholder: "e.g. TypeScript Expert",
              value: form.name,
              disabled: !editable,
              autoFocus: !skill,
              onChange: (name) => setForm({ ...form, name }),
            },
            {
              kind: "text",
              id: "skill-body",
              label: "Body",
              placeholder: "You are an expert TypeScript developer…",
              value: form.body,
              multiline: true,
              prose: true,
              lines: [6, 16],
              disabled: !editable,
              onChange: (body) => setForm({ ...form, body }),
            },
          ],
        },
        ...(!skill && isAdmin
          ? [
              {
                id: "scope",
                footer:
                  "Shared skills apply to every member of this organization.",
                rows: [
                  {
                    kind: "toggle" as const,
                    id: "skill-shared",
                    label: "Share with entire organization",
                    value: form.shared,
                    onChange: (shared: boolean) => setForm({ ...form, shared }),
                  },
                ],
              },
            ]
          : []),
        ...(skill && editable
          ? [
              {
                id: "delete",
                rows: [
                  {
                    kind: "button" as const,
                    id: "delete-skill",
                    title: "Delete skill",
                    symbol: "trash" as const,
                    destructive: true,
                    busy: busy === "delete",
                    disabled: Boolean(busy),
                    onPress: remove,
                  },
                ],
              },
            ]
          : []),
      ]
    : [];

  return (
    <SettingsPage
      title={skill ? skill.name : "New skill"}
      sections={sections}
      loading={!form && !skills.data && !skills.error}
      empty={{
        title: "Skill unavailable",
        description: skills.error ?? "This skill no longer exists.",
        symbol: "exclamationmark.triangle.fill",
      }}
      primaryAction={
        editable
          ? {
              title: skill ? "Save" : "Create",
              onPress: save,
              busy: busy === "save",
              disabled: !valid || Boolean(busy),
              testID: "save-skill",
            }
          : undefined
      }
      testID="settings-skill"
    />
  );
}
