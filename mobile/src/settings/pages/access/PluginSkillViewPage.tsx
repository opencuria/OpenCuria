import React from "react";
import { useResource } from "../../resources";
import { useSettings } from "../../SettingsContext";
import { TextPage } from "../TextPage";
import { PLUGINS } from "./resources";

/** A skill of an installed plugin, read-only. */
export function PluginSkillViewPage({
  pluginId,
  skillId,
}: {
  pluginId: string;
  skillId: string;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const plugins = useResource(resources, PLUGINS, () => client.plugins.list());
  const skill = plugins.data
    ?.find((entry) => entry.id === pluginId)
    ?.skills?.find((entry) => entry.id === skillId);
  return (
    <TextPage
      title={skill?.name ?? "Skill"}
      label="Markdown"
      text={plugins.data ? (skill?.body ?? "") : undefined}
      monospace={false}
      loading={!plugins.data}
      error={plugins.error}
      onRetry={() => void plugins.reload()}
      emptyText="This skill no longer exists."
      testID="settings-plugin-skill-view"
    />
  );
}
