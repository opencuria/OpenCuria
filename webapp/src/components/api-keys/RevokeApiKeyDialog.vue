<script setup lang="ts">
import { ref, watch } from 'vue'
import { useApiKeyStore } from '@/stores/apiKeys'
import type { APIKey } from '@/types'
import { TriangleAlert } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

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
  <Dialog :open="open" @update:open="(v) => !v && emit('close')">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Revoke API Key</DialogTitle>
      </DialogHeader>

      <div class="flex flex-col gap-4">
        <div class="flex items-start gap-3 rounded-[var(--radius-md)] border border-destructive/30 bg-destructive/10 px-3.5 py-3">
          <TriangleAlert :size="16" class="mt-0.5 shrink-0 text-destructive" />
          <div class="text-sm text-destructive">
            <p class="font-medium">This action is irreversible.</p>
            <p class="text-destructive/80 mt-0.5">
              Any integration using <span class="font-mono font-medium">{{ apiKey?.key_prefix }}…</span>
              will immediately lose access.
            </p>
          </div>
        </div>

        <p class="text-sm text-muted-foreground">
          Are you sure you want to revoke <span class="font-medium text-foreground">{{ apiKey?.name }}</span>?
        </p>

        <div class="flex justify-end gap-2">
          <Button variant="outline" :disabled="submitting" @click="emit('close')">Cancel</Button>
          <Button
            variant="destructive"
            :disabled="submitting"
            @click="handleRevoke"
          >
            {{ submitting ? 'Revoking…' : 'Revoke Key' }}
          </Button>
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>
