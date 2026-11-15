<script setup lang="ts">
import CopyButton from '@/components/common/CopyButton.vue'

withDefaults(
  defineProps<{
    code: string
    language?: string
    preClass?: string
    codeClass?: string
    disabled?: boolean
  }>(),
  { language: '', preClass: '', codeClass: '', disabled: false },
)
</script>

<template>
  <div class="group/code relative" data-testid="harness-code-block">
    <CopyButton
      :text="code"
      label="code"
      :disabled="disabled"
      data-testid="code-copy"
      class="absolute right-1 top-1 z-10 text-muted-foreground opacity-0 transition-opacity group-hover/code:opacity-100 focus:opacity-100 [@media(hover:none)]:opacity-100"
    />
    <pre
      :class="preClass"
    ><code :class="[codeClass, language && !codeClass.split(/\s+/).includes(`language-${language}`) ? `language-${language}` : undefined]">{{ code }}</code></pre>
  </div>
</template>
