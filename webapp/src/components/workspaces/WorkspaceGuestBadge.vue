<script setup lang="ts">
/**
 * "Windows · Activated" for Windows workspaces; Linux workspaces render nothing.
 * `compact` drops the OS where the image already names it (workspace cards).
 */
import { computed } from 'vue'
import { Badge } from '@/components/ui/badge'
import { workspaceWindowsView, type WindowsLicenseTone } from '@opencuria/chat-core/windowsLicense'
import type { Workspace } from '@/types'

const props = defineProps<{ workspace: Workspace; compact?: boolean }>()

const VARIANTS: Record<WindowsLicenseTone, 'secondary' | 'outline' | 'destructive'> = {
  ok: 'secondary',
  busy: 'outline',
  attention: 'destructive',
  muted: 'outline',
}

const view = computed(() => workspaceWindowsView(props.workspace))
const variant = computed(() =>
  view.value?.license ? VARIANTS[view.value.license.tone] : 'outline',
)
const text = computed(() =>
  props.compact && view.value?.license ? view.value.license.label : view.value?.text,
)
</script>

<template>
  <Badge
    v-if="view"
    :variant="variant"
    class="min-w-0 max-w-full shrink"
    :title="view.text"
    data-testid="workspace-guest-badge"
  >
    <span class="truncate">{{ text }}</span>
  </Badge>
</template>
