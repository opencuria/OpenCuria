import React from "react";
import type { Skill } from "@opencuria/client";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { useResource } from "../../resources";
import { route } from "../../routes";
import { loadErrorSections } from "../../sections";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";

export const SKILLS = "skills";

/** First non-empty line of a skill's Markdown, without heading marks. */
export function skillPreview(body: string): string {
  const line = body
    .split("\n")
    .map((entry) => entry.replace(/^#+\s*/, "").trim())
    .find(Boolean);
  return line ?? "";
}

/** Organization members can edit personal skills; organization skills need an admin. */
export function canEditSkill(
  skill: Pick<Skill, "scope">,
  isAdmin: boolean,
): boolean {
  return skill.scope === "personal" || isAdmin;
}

/** Skills: reusable prompt fragments, personal or shared with the organization. */
export function SkillsPage(): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const skills = useResource(resources, SKILLS, () => client.skills.list());
  const groups: Array<[string, Skill["scope"]]> = [
    ["Organization", "organization"],
    ["Personal", "personal"],
  ];
  const sections: NativeFormSection[] = groups
    .map(([title, scope]) => ({
      id: scope,
      title,
      rows: (skills.data ?? [])
        .filter((skill) => skill.scope === scope)
        .map((skill) => ({
          kind: "item" as const,
          id: `skill-${skill.id}`,
          testID: `skill-${skill.id}`,
          title: skill.name,
          subtitle: skillPreview(skill.body),
          onPress: () => navigation.push(route("skill", skill.id)),
        })),
    }))
    .filter((section) => section.rows.length);
  if (sections.length)
    sections[sections.length - 1]!.footer =
      "Appended to harness prompts. Personal skills are yours across all organizations; organization skills are shared with all members.";

  return (
    <SettingsPage
      title="Skills"
      sections={
        skills.error && !skills.data
          ? loadErrorSections(skills.error, () => void skills.reload())
          : sections
      }
      loading={!skills.data && !skills.error}
      onRefresh={skills.reload}
      empty={{
        title: "No skills yet",
        description:
          "Create your first skill to inject reusable prompt context into harness sessions.",
        symbol: "book.closed",
      }}
      primaryAction={{
        title: "New skill",
        symbol: "plus",
        onPress: () => navigation.push(route("skill", "new")),
        testID: "new-skill",
      }}
      testID="settings-skills"
    />
  );
}
