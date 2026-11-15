<script setup lang="ts">
/**
 * Version history of one image (captured image or runner build), newest first:
 * version, message, size, retention state and how many workspaces use it.
 * Which workspaces those are stays private to their owners. Missing messages
 * and sizes are left out rather than shown as placeholders.
 */
import { Trash2 } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { retentionLabel } from '@opencuria/chat-core/imageVersions'
import { formatDate } from '@/lib/utils'
import { formatStorageBytes } from '@opencuria/chat-core/workspaceDisk'
import type { ImageArtifact } from '@/types'

defineProps<{
  versions: ImageArtifact[]
  /** Latest versions can only be removed together with the image. */
  canDelete?: boolean
}>()

const emit = defineEmits<{ delete: [version: ImageArtifact] }>()

function badgeVariant(version: ImageArtifact): 'default' | 'secondary' | 'outline' | 'destructive' {
  if (version.status === 'failed') return 'destructive'
  if (version.retention === 'latest') return 'default'
  if (version.retention === 'expires_when_unused') return 'outline'
  return 'secondary'
}

function deletable(version: ImageArtifact): boolean {
  return (
    !version.is_latest &&
    !['capturing', 'building', 'pending_deletion', 'deleting', 'deleted'].includes(version.status)
  )
}

/** "Sep 6, 07:07 AM · 2.1 GiB · Used by 2 workspaces"; unknown sizes are left out. */
function details(version: ImageArtifact): string {
  const count = version.workspace_count ?? 0
  const used = count ? `Used by ${count} ${count === 1 ? 'workspace' : 'workspaces'}` : ''
  return [
    formatDate(version.created_at),
    version.size_bytes == null ? '' : formatStorageBytes(version.size_bytes),
    used,
  ]
    .filter(Boolean)
    .join(' · ')
}
</script>

<template>
  <ol class="divide-y divide-border" data-testid="image-version-list">
    <li
      v-for="version in versions"
      :key="version.id"
      class="flex items-start justify-between gap-3 py-2"
      data-testid="image-version-row"
    >
      <div class="min-w-0 space-y-0.5">
        <div class="flex flex-wrap items-center gap-1.5 text-sm">
          <span v-if="version.version" class="font-medium text-foreground"
            >v{{ version.version }}</span
          >
          <Badge v-if="retentionLabel(version)" :variant="badgeVariant(version)">
            {{ retentionLabel(version) }}
          </Badge>
          <Badge v-if="['capturing', 'building'].includes(version.status)" variant="outline">
            {{ version.status === 'building' ? 'Building…' : 'Capturing…' }}
          </Badge>
        </div>
        <p v-if="version.message" class="text-xs text-foreground/80 break-words">
          {{ version.message }}
        </p>
        <p class="text-xs text-muted-foreground">{{ details(version) }}</p>
      </div>
      <Button
        v-if="canDelete && deletable(version)"
        variant="ghost"
        size="icon-sm"
        class="text-destructive hover:text-destructive"
        :title="version.version ? `Delete v${version.version}` : 'Delete version'"
        :aria-label="version.version ? `Delete v${version.version}` : 'Delete version'"
        data-testid="image-version-delete"
        @click="emit('delete', version)"
      >
        <Trash2 />
      </Button>
    </li>
  </ol>
</template>
