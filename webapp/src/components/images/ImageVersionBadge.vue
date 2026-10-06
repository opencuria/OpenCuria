<script setup lang="ts">
/** "image · v3" chip for a workspace, with an update hint when a newer version exists. */
import { computed } from 'vue'
import { Layers } from '@lucide/vue'
import { versionLabel } from '@/lib/imageVersions'
import type { Workspace } from '@/types'

const props = defineProps<{ workspace: Workspace }>()

const emit = defineEmits<{ update: [] }>()

const base = computed(() => props.workspace.base_image)
const label = computed(() => versionLabel(base.value) ?? props.workspace.base_image_name ?? null)
const pending = computed(() => props.workspace.pending_base_image)
const title = computed(() => {
  if (!label.value) return ''
  const message = base.value?.message ? ` — ${base.value.message}` : ''
  return `Based on ${label.value}${message}`
})
</script>

<template>
  <div v-if="label" class="mt-1 flex max-w-full flex-wrap items-center gap-1">
    <span
      class="inline-flex max-w-full items-center gap-1 rounded-[var(--radius-sm)] bg-muted/60 px-1.5 py-0.5 text-[11px] text-muted-foreground"
      :title="title"
      data-testid="image-version-badge"
    >
      <Layers :size="11" class="shrink-0" />
      <span class="truncate">{{ label }}</span>
      <span v-if="pending && pending.id !== base?.id" class="shrink-0">→ v{{ pending.version }}</span>
    </span>
    <button
      v-if="base?.update_available && !pending"
      type="button"
      class="rounded-[var(--radius-sm)] px-1.5 py-0.5 text-[11px] text-primary transition-colors hover:bg-primary/10"
      :title="`Update to v${base.latest_version}`"
      data-testid="image-version-update"
      @click.stop="emit('update')"
    >
      v{{ base.latest_version }} available
    </button>
  </div>
  <div v-else class="mt-1 text-xs text-muted-foreground">—</div>
</template>
