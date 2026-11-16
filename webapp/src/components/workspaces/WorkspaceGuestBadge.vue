<script setup lang="ts">
/** "Windows · Activated" for Windows workspaces; Linux workspaces render nothing. */
import { computed } from 'vue'
import { Badge } from '@/components/ui/badge'
import { workspaceWindowsView, type WindowsLicenseTone } from '@opencuria/chat-core/windowsLicense'
import type { Workspace } from '@/types'

const props = defineProps<{ workspace: Workspace }>()

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
</script>

<template>
  <Badge
    v-if="view"
    :variant="variant"
    class="min-w-0 max-w-full"
    :title="view.text"
    data-testid="workspace-guest-badge"
  >
    <span class="truncate">{{ view.text }}</span>
  </Badge>
</template>
