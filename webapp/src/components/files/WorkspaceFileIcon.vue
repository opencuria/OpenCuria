<script setup lang="ts">
/**
 * WorkspaceFileIcon — VS Code-like colored file/folder SVG icon.
 *
 * Icons are a small curated subset of `material-icon-theme` (MIT) copied
 * into `src/assets/file-icons/` (see `src/lib/fileIcons.ts` for the mapping
 * and attribution). Rendered via `<img>` so the multicolor SVG fills are
 * preserved as-is.
 *
 * Props:
 * - `path` / `name`: at least one identifies the file (basename is used).
 * - `directory`: render a folder icon (resolved from the folder name).
 * - `expanded`: open-folder variant for directories.
 * - `size`: pixel width/height (default 16).
 */
import { computed } from 'vue'
import { resolveWorkspaceIconKey } from '@/lib/fileIcons'
import { workspaceIconUrl } from '@/lib/fileIconAssets'

const props = withDefaults(
  defineProps<{
    path?: string | null
    name?: string | null
    directory?: boolean
    expanded?: boolean
    size?: number
  }>(),
  {
    path: null,
    name: null,
    directory: false,
    expanded: false,
    size: 16,
  },
)

const iconKey = computed(() =>
  resolveWorkspaceIconKey({
    path: props.path,
    name: props.name,
    directory: props.directory,
    expanded: props.expanded,
  }),
)

const src = computed(() => workspaceIconUrl(iconKey.value, props.directory, props.expanded))

const dimension = computed(() => `${props.size}px`)
</script>

<template>
  <img
    :src="src"
    :width="size"
    :height="size"
    :style="{ width: dimension, height: dimension }"
    alt=""
    aria-hidden="true"
    draggable="false"
    data-testid="workspace-file-icon"
    :data-icon="iconKey"
    class="shrink-0 select-none object-contain"
  />
</template>
