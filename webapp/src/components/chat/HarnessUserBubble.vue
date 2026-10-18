<script setup lang="ts">
/**
 * HarnessUserBubble — the right-aligned user message bubble with its
 * `@file:` / `@agent:` mention badges and image thumbnails.
 *
 * Shared by the real user message and the steer row inside an assistant turn
 * so both stay one visual family. Renders a fragment (bubble + thumbnail
 * strip); the parent owns alignment and surrounding actions.
 */
import { inject, ref } from 'vue'
import type { ComposerFileToken } from '@/lib/composerTokens'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import HarnessMarkdown from './HarnessMarkdown.vue'
import HarnessMentionImages from './HarnessMentionImages.vue'

defineProps<{ text: string }>()

const workspaceId = inject(harnessWorkspaceIdKey, ref(''))
const mentionImages = ref<ComposerFileToken[]>([])
</script>

<template>
  <div
    class="overflow-x-auto rounded-2xl rounded-br-md border border-border/70 bg-muted/60 px-3.5 py-2.5 text-sm text-foreground break-words"
    data-testid="harness-user-bubble"
  >
    <HarnessMarkdown :text="text" compact mentions @mention-images="mentionImages = $event" />
  </div>
  <HarnessMentionImages
    v-if="mentionImages.length"
    :tokens="mentionImages"
    :workspace-id="workspaceId"
    strip-class="mt-1.5 justify-end"
  />
</template>
