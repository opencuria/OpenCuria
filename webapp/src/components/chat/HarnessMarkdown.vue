<script setup lang="ts">
import { computed } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import type { HarnessPart } from '@/types/harness'

const props = defineProps<{
  text: string
  compact?: boolean
}>()

/** Render harness markdown (marked + DOMPurify, links open in a new tab). */
function renderMarkdown(text: string): string {
  if (!text) return ''
  const rawHtml = marked.parse(text) as string
  const sanitized = DOMPurify.sanitize(rawHtml, {
    ADD_DATA_URI_TAGS: ['img'],
    ALLOWED_URI_REGEXP: /^(?:(?:https?|ftp|mailto|tel|file|data|blob):|[^a-z]|[a-z+.-]+(?:[^a-z+.-:]|$))/i,
  })
  if (typeof DOMParser === 'undefined') return sanitized
  const parser = new DOMParser()
  const doc = parser.parseFromString(`<div id="md-root">${sanitized}</div>`, 'text/html')
  const root = doc.getElementById('md-root')
  if (!root) return sanitized
  for (const link of Array.from(root.querySelectorAll('a[href]'))) {
    link.setAttribute('target', '_blank')
    link.setAttribute('rel', 'noopener noreferrer')
  }
  return root.innerHTML
}

const rendered = computed(() => renderMarkdown(props.text))

defineExpose({ renderMarkdown })
export type { HarnessPart }
</script>

<template>
  <div
    class="prose-output prose prose-sm max-w-3xl break-words prose-p:leading-relaxed prose-pre:p-2 prose-pre:bg-muted prose-pre:text-muted-foreground prose-pre:rounded-md dark:prose-invert prose-headings:text-foreground prose-p:text-foreground prose-strong:text-foreground prose-ul:text-foreground prose-ol:text-foreground prose-li:text-foreground prose-a:text-primary prose-code:text-foreground prose-blockquote:text-muted-foreground prose-blockquote:border-l-primary"
    :class="compact ? 'text-[13px]' : ''"
    v-html="rendered"
  ></div>
</template>
