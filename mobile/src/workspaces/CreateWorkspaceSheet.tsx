import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  compatibleRunners,
  defaultWorkspaceImageOption,
  definitionArtifactForRunner,
  fitQemuResources,
  qemuResourceLimits,
  workspaceImageOptions,
  type QemuResourceLimits,
  type QemuResources,
} from "@opencuria/chat-core/workspaceCreation";
import type { OpenCuriaClient } from "@opencuria/client";
import { NativeForm } from "../ui/NativeForm";
import type { NativeFormRow, NativeFormSection } from "../ui/NativeForm.types";
import { NativeModal } from "../ui/primitives";
import { showToast } from "../ui/toast";
import type { WorkspaceCatalog } from "./useWorkspaceCatalog";
import {
  pluginCredentialSections,
  resourceRows,
  resourcesValid,
} from "./workspaceFormSections";
import { errorText } from "./workspaceOperations";

export interface CreateWorkspaceSheetProps {
  visible: boolean;
  client: OpenCuriaClient;
  catalog: WorkspaceCatalog;
  organizationId: string | null;
  onClose: () => void;
  /** The server accepted the workspace; it is provisioning now. */
  onCreated: (workspaceId: string) => Promise<void>;
  /** Preselected image option, e.g. `captured:<latest version id>`. */
  initialImageValue?: string;
}

function repoName(url: string): string {
  return (
    url
      .replace(/\/+$/, "")
      .split("/")
      .pop()
      ?.replace(/\.git$/, "") || url
  );
}

/** "New workspace": name and image first; repositories, plugins and resources below. */
export function CreateWorkspaceSheet(
  props: CreateWorkspaceSheetProps,
): React.JSX.Element | null {
  if (!props.visible) return null;
  return <CreateSheet {...props} />;
}

function CreateSheet({
  client,
  catalog,
  organizationId,
  onClose,
  onCreated,
  initialImageValue = "",
}: CreateWorkspaceSheetProps): React.JSX.Element {
  const [name, setName] = useState("");
  const [imageValue, setImageValue] = useState(initialImageValue);
  const [runnerId, setRunnerId] = useState("");
  const [resources, setResources] = useState<QemuResources | null>(null);
  const [repos, setRepos] = useState<string[]>([]);
  const [repoInput, setRepoInput] = useState("");
  const [pluginIds, setPluginIds] = useState<string[]>([]);
  const [credentialIds, setCredentialIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);

  const options = useMemo(
    () =>
      workspaceImageOptions({
        definitions: catalog.definitions,
        buildsByDefinition: catalog.buildsByDefinition,
        capturedImages: catalog.capturedImages,
        runners: catalog.runners,
      }),
    [
      catalog.definitions,
      catalog.buildsByDefinition,
      catalog.capturedImages,
      catalog.runners,
    ],
  );
  const all = [...options.definitions, ...options.captured];
  const selected =
    all.find((option) => option.value === imageValue) ??
    defaultWorkspaceImageOption(options);
  const captured = selected?.kind === "captured";
  const runners = compatibleRunners(selected, catalog.runners);
  const runner =
    runners.find((entry) => entry.id === runnerId) ?? runners[0] ?? null;
  const limits = qemuResourceLimits(runner);
  const previousLimits = useRef<QemuResourceLimits>(limits);
  const effective = resources ?? {
    vcpus: limits.defaultVcpus,
    memoryMb: limits.defaultMemoryMb,
    diskSizeGb: limits.defaultDiskSizeGb,
  };

  // Resources follow the runner's defaults unless customized and still valid.
  const limitsKey = JSON.stringify(limits);
  useEffect(() => {
    const previous = previousLimits.current;
    previousLimits.current = JSON.parse(limitsKey) as QemuResourceLimits;
    setResources((current) =>
      current
        ? fitQemuResources(current, previous, previousLimits.current)
        : null,
    );
  }, [limitsKey]);

  const chooseImage = (value: string) => {
    if (value === selected?.value) return;
    setImageValue(value);
    setRunnerId("");
    // Plugins and credentials depend on the image, as in the webapp.
    setPluginIds([]);
    setCredentialIds([]);
  };

  const addRepo = (): string[] => {
    const trimmed = repoInput.trim();
    if (!trimmed || repos.includes(trimmed)) return repos;
    const next = [...repos, trimmed];
    setRepos(next);
    setRepoInput("");
    return next;
  };

  const selection = pluginCredentialSections({
    catalog,
    organizationId,
    pluginIds,
    credentialIds,
    onPluginIds: setPluginIds,
    onCredentialIds: setCredentialIds,
    disabled: submitting,
  });
  const artifactId = definitionArtifactForRunner(selected, runner?.id);
  const valid =
    Boolean(name.trim()) &&
    Boolean(selected) &&
    selection.valid &&
    (captured || (Boolean(artifactId) && resourcesValid(effective, limits)));

  const create = async () => {
    if (!valid || !selected || submitting) return;
    setSubmitting(true);
    try {
      const result =
        selected.kind === "captured"
          ? await client.images.createWorkspace(selected.capturedImageId, {
              name: name.trim(),
              credential_ids: credentialIds,
              plugin_ids: pluginIds,
            })
          : await client.workspaces.create({
              name: name.trim(),
              image_id: artifactId!,
              repos: addRepo(),
              credential_ids: credentialIds,
              plugin_ids: pluginIds,
              runner_id: runner?.id ?? null,
              qemu_vcpus: effective.vcpus,
              qemu_memory_mb: effective.memoryMb,
              qemu_disk_size_gb: effective.diskSizeGb,
            });
      // The parent closes this sheet; the toast then shows on the list.
      await onCreated(result.workspace_id);
      showToast({
        title: "Creating workspace",
        message: "Provisioning started. This can take a moment.",
      });
    } catch (cause) {
      showToast({
        title: "Could not create workspace",
        message: errorText(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const imageRows: NativeFormRow[] = all.length
    ? [
        {
          kind: "picker",
          id: "image",
          label: "Image",
          value: selected?.value ?? "",
          groups: [
            ...(options.definitions.length
              ? [
                  {
                    title: "Organization images",
                    options: options.definitions.map((option) => ({
                      value: option.value,
                      label: option.label,
                    })),
                  },
                ]
              : []),
            ...(options.captured.length
              ? [
                  {
                    title: "Captured images",
                    options: options.captured.map((option) => ({
                      value: option.value,
                      label: option.label,
                    })),
                  },
                ]
              : []),
          ],
          onChange: chooseImage,
          disabled: submitting,
        },
      ]
    : [
        {
          kind: "note",
          id: "no-images",
          text: catalog.loading
            ? "Loading images…"
            : (catalog.imageError ??
              "No organization or captured images are available on an online runner."),
          tone: catalog.loading ? "muted" : "danger",
        },
      ];

  const sections: NativeFormSection[] = [
    {
      id: "workspace",
      rows: [
        {
          kind: "text",
          id: "workspace-name",
          label: "Name",
          placeholder: "Name, e.g. billing-service",
          value: name,
          onChange: setName,
          disabled: submitting,
        },
        ...imageRows,
      ],
      footer: captured
        ? "Clones of a captured image keep its runner and resources."
        : undefined,
    },
    ...(captured
      ? []
      : [
          {
            id: "repositories",
            title: "Repositories",
            rows: [
              ...repos.map(
                (repo): NativeFormRow => ({
                  kind: "item",
                  id: `repo-${repo}`,
                  title: repoName(repo),
                  subtitle: repo,
                  actions: [
                    {
                      id: "remove",
                      title: "Remove",
                      symbol: "trash",
                      destructive: true,
                    },
                  ],
                  trailingSwipe: [
                    {
                      id: "remove",
                      title: "Remove",
                      symbol: "trash",
                      destructive: true,
                    },
                  ],
                  onAction: () =>
                    setRepos((current) =>
                      current.filter((entry) => entry !== repo),
                    ),
                }),
              ),
              {
                kind: "text",
                id: "repo-input",
                label: "Repository URL",
                placeholder: "https://github.com/owner/repo",
                keyboard: "url",
                value: repoInput,
                onChange: setRepoInput,
                onSubmit: () => {
                  addRepo();
                },
                disabled: submitting,
              },
              {
                kind: "button",
                id: "repo-add",
                title: "Add repository",
                symbol: "plus",
                disabled: !repoInput.trim() || submitting,
                onPress: () => {
                  addRepo();
                },
              },
            ],
            footer:
              "Optional. Cloned into /workspace when the workspace is created.",
          } satisfies NativeFormSection,
        ]),
    ...selection.sections,
    ...(captured
      ? []
      : [
          {
            id: "resources",
            title: "Resources",
            rows: [
              runners.length > 1
                ? {
                    kind: "picker",
                    id: "runner",
                    label: "Runner",
                    value: runner?.id ?? "",
                    groups: [
                      {
                        options: runners.map((entry) => ({
                          value: entry.id,
                          label: entry.name || entry.id.slice(0, 8),
                        })),
                      },
                    ],
                    onChange: setRunnerId,
                    disabled: submitting,
                  }
                : {
                    kind: "value",
                    id: "runner",
                    label: "Runner",
                    value: runner ? runner.name || runner.id.slice(0, 8) : "—",
                  },
              ...resourceRows({
                resources: effective,
                limits,
                onChange: setResources,
                disabled: submitting || !runner,
              }),
            ],
            footer: "Only online runners with the selected image are listed.",
          } satisfies NativeFormSection,
        ]),
  ];

  return (
    <NativeModal
      visible
      onClose={onClose}
      title="New workspace"
      scrollable={false}
      swipeToDismiss={!name.trim() && !repos.length}
      primaryAction={{
        title: captured ? "Clone" : "Create",
        onPress: () => {
          void create();
        },
        disabled: !valid,
        busy: submitting,
        testID: "workspace-create",
      }}
      testID="create-workspace-sheet"
    >
      <NativeForm sections={sections} />
    </NativeModal>
  );
}
