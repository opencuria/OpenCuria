<script setup lang="ts">
import { ref, watch } from 'vue'
import { UiDialog, UiButton } from '@/components/ui'
import { useApiKeyStore } from '@/stores/apiKeys'
import type { APIKey } from '@/types'
import { TriangleAlert } from 'lucide-vue-next'

const props = defineProps<{
  apiKey: APIKey | null
}>()

const emit = defineEmits<{
  close: []
}>()

const apiKeyStore = useApiKeyStore()
const submitting = ref(false)
const open = ref(false)

watch(
  () => props.apiKey,
  (val) => {
    open.value = val !== null
  },
)

async function handleRevoke(): Promise<void> {
  if (!props.apiKey) return
  submitting.value = true
  const ok = await apiKeyStore.revokeKey(props.apiKey.id)
  submitting.value = false
  if (ok) emit('close')
}
</script>

<template>
  <UiDialog
    :open="open"
    title="Revoke API Key"
    @update:open="(v) => !v && emit('close')"
  >
    <div class="flex flex-col gap-4">
      <div class="flex items-start gap-3 rounded-[var(--radius-md)] border border-error/30 bg-error-muted px-3.5 py-3">
        <TriangleAlert :size="16" class="mt-0.5 shrink-0 text-error" />
        <div class="text-sm text-error">
          <p class="font-medium">This action is irreversible.</p>
          <p class="text-error/80 mt-0.5">
            Any integration using <span class="font-mono font-medium">{{ apiKey?.key_prefix }}…</span>
            will immediately lose access.
          </p>
        </div>
      </div>

      <p class="text-sm text-muted-fg">
        Are you sure you want to revoke <span class="font-medium text-fg">{{ apiKey?.name }}</span>?
      </p>

      <div class="flex justify-end gap-2">
        <UiButton variant="outline" :disabled="submitting" @click="emit('close')">Cancel</UiButton>
        <UiButton
          variant="destructive"
          :disabled="submitting"
          @click="handleRevoke"
        >
          {{ submitting ? 'Revoking…' : 'Revoke Key' }}
        </UiButton>
      </div>
    </div>
  </UiDialog>
</template>
