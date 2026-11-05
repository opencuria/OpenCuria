import React, { useEffect, useState } from "react";
import {
  BASE_DISTRO_PRESETS,
  envVarsTextValid,
  imageDefinitionForm,
  imageDefinitionPayload,
  isImageDefinitionFormValid,
  type ImageDefinitionForm,
  visibleImageDefinitions,
} from "@opencuria/chat-core/imageDefinitionLifecycle";
import type { NativeFormSection } from "../../../ui/NativeForm.types";
import { showToast } from "../../../ui/toast";
import { errorMessage, useResource } from "../../resources";
import { route } from "../../routes";
import { useSettings, useSettingsNavigation } from "../../SettingsContext";
import { SettingsPage } from "../../SettingsPage";
import { IMAGE_DEFINITIONS } from "./ImageDefinitionsPage";

/** Creates or edits an image definition (admins). */
export function ImageDefinitionEditorPage({
  definitionId,
}: {
  /** `null` creates a definition. */
  definitionId: string | null;
}): React.JSX.Element {
  const { client, resources } = useSettings();
  const navigation = useSettingsNavigation();
  const definitions = useResource(resources, IMAGE_DEFINITIONS, async () =>
    visibleImageDefinitions(await client.images.listDefinitions()),
  );
  const definition =
    definitionId === null
      ? null
      : definitions.data?.find((entry) => entry.id === definitionId);
  const [form, setForm] = useState<ImageDefinitionForm | null>(
    definitionId === null ? imageDefinitionForm() : null,
  );
  useEffect(() => {
    if (definition && !form) setForm(imageDefinitionForm(definition));
  }, [definition, form]);
  const [busy, setBusy] = useState(false);
  const edit = (next: Partial<ImageDefinitionForm>) =>
    setForm((current) => (current ? { ...current, ...next } : current));

  const save = async () => {
    if (!form || !isImageDefinitionFormValid(form)) return;
    setBusy(true);
    try {
      const payload = imageDefinitionPayload(form);
      if (definition) {
        const saved = await client.images.updateDefinition(
          definition.id,
          payload,
        );
        definitions.set((current) =>
          (current ?? []).map((entry) =>
            entry.id === saved.id ? saved : entry,
          ),
        );
        navigation.pop();
      } else {
        const created = await client.images.createDefinition(payload);
        definitions.set((current) => [...(current ?? []), created]);
        navigation.replace(route("image-definition", created.id));
      }
      showToast({
        title: definition ? "Definition saved" : "Definition created",
        tone: "success",
      });
    } catch (cause) {
      showToast({
        title: "Could not save the definition",
        message: errorMessage(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const envValid = form ? envVarsTextValid(form.envVars) : true;
  const sections: NativeFormSection[] = form
    ? [
        {
          id: "definition",
          footer:
            "QEMU image builds currently support Ubuntu cloud images only.",
          rows: [
            {
              kind: "text",
              id: "name",
              label: "Name",
              placeholder: "Python Dev Environment",
              value: form.name,
              autoFocus: !definition,
              onChange: (name) => edit({ name }),
            },
            {
              kind: "text",
              id: "description",
              label: "Description",
              placeholder: "Description",
              value: form.description,
              multiline: true,
              prose: true,
              lines: [2, 4],
              onChange: (description) => edit({ description }),
            },
            {
              kind: "picker",
              id: "base-distro",
              label: "Base distro",
              value: form.baseDistroPreset,
              groups: [
                {
                  options: [
                    ...BASE_DISTRO_PRESETS.map((value) => ({
                      value,
                      label: value,
                    })),
                    { value: "custom", label: "Custom…" },
                  ],
                },
              ],
              onChange: (baseDistroPreset) => edit({ baseDistroPreset }),
            },
            ...(form.baseDistroPreset === "custom"
              ? [
                  {
                    kind: "text" as const,
                    id: "custom-distro",
                    label: "Custom base distro",
                    placeholder: "e.g. ubuntu:22.04",
                    monospace: true,
                    value: form.customBaseDistro,
                    onChange: (customBaseDistro: string) =>
                      edit({ customBaseDistro }),
                  },
                ]
              : []),
          ],
        },
        {
          id: "packages",
          title: "Packages and environment",
          footer: envValid
            ? "Packages are comma-separated. One KEY=VALUE per line for the environment."
            : "Each environment line needs a KEY before its “=”.",
          footerTone: envValid ? "muted" : "danger",
          rows: [
            {
              kind: "text",
              id: "packages",
              label: "Pre-installed packages",
              placeholder: "python3, nodejs, ffmpeg",
              value: form.packages,
              onChange: (packages) => edit({ packages }),
            },
            {
              kind: "text",
              id: "env-vars",
              label: "Baked environment variables",
              placeholder: "NODE_ENV=production",
              multiline: true,
              monospace: true,
              lines: [3, 8],
              value: form.envVars,
              onChange: (envVars) => edit({ envVars }),
            },
          ],
        },
        {
          id: "advanced",
          title: "Advanced",
          footer: "The base distro and packages above are applied first.",
          rows: [
            {
              kind: "text",
              id: "init-script",
              label: "Custom init script",
              placeholder: "#!/bin/bash",
              multiline: true,
              monospace: true,
              lines: [4, 14],
              value: form.customInitScript,
              onChange: (customInitScript) => edit({ customInitScript }),
            },
          ],
        },
      ]
    : [];

  return (
    <SettingsPage
      title={definition ? "Edit definition" : "New definition"}
      sections={sections}
      loading={!form && !definitions.data && !definitions.error}
      empty={{
        title: "Definition unavailable",
        description: definitions.error ?? "This definition no longer exists.",
        symbol: "exclamationmark.triangle.fill",
      }}
      primaryAction={{
        title: definition ? "Save" : "Create",
        onPress: () => void save(),
        busy,
        disabled: !form || !isImageDefinitionFormValid(form) || busy,
        testID: "save-image-definition",
      }}
      testID="settings-image-definition-editor"
    />
  );
}
