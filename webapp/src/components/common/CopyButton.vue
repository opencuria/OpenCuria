<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { Check, Copy } from '@lucide/vue'
import { Button, type ButtonVariants } from '@/components/ui/button'
import { copyToClipboard, type ClipboardText } from '@/lib/clipboard'

const props = withDefaults(
  defineProps<{
    text: ClipboardText
    label: string
    disabled?: boolean
    variant?: ButtonVariants['variant']
    size?: ButtonVariants['size']
  }>(),
  { variant: 'ghost', size: 'icon-sm' },
)

const copied = ref(false)
const busy = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let controller: AbortController | undefined
function reset(): void {
  clearTimeout(timer)
  controller?.abort()
  copied.value = false
  busy.value = false
}
watch(() => [props.text, props.label, props.disabled] as const, reset)
onBeforeUnmount(reset)

async function copy(): Promise<void> {
  if (props.disabled || busy.value) return
  const operation = new AbortController()
  controller = operation
  busy.value = true
  copied.value = false
  clearTimeout(timer)
  const ok = await copyToClipboard(props.text, props.label, {
    success: 'silent',
    signal: operation.signal,
  })
  if (operation.signal.aborted) return
  busy.value = false
  copied.value = ok
  if (ok)
    timer = setTimeout(() => {
      copied.value = false
    }, 2000)
}
</script>

<template>
  <Button
    type="button"
    :variant="variant"
    :size="size"
    :disabled="disabled || busy"
    :aria-label="copied ? 'Copied' : `Copy ${label}`"
    :title="copied ? 'Copied' : `Copy ${label}`"
    :data-copied="copied"
    @click.stop="copy"
  >
    <Check v-if="copied" :size="13" class="text-success" />
    <Copy v-else :size="13" />
    <slot>{{ size?.startsWith('icon') ? '' : copied ? 'Copied' : `Copy ${label}` }}</slot>
  </Button>
</template>
