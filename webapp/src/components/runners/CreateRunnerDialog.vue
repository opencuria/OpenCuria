<script setup lang="ts">
import { ref } from 'vue'
import { UiDialog, UiInput, UiButton } from '@/components/ui'
import { useRunnerStore } from '@/stores/runners'
import { Copy, Check } from 'lucide-vue-next'

const runnerStore = useRunnerStore()

const open = ref(false)
const name = ref('')
const createdToken = ref<string | null>(null)
const copied = ref(false)
const submitting = ref(false)

async function handleSubmit(): Promise<void> {
  submitting.value = true
  const result = await runnerStore.createRunner(name.value)
  submitting.value = false

  if (result) {
    createdToken.value = result.api_token
  }
}

function handleCopyToken(): void {
  if (!createdToken.value) return
  navigator.clipboard.writeText(createdToken.value)
  copied.value = true
  setTimeout(() => (copied.value = false), 2000)
}

function handleClose(): void {
  open.value = false
  // Reset state after dialog close animation
  setTimeout(() => {
    name.value = ''
    createdToken.value = null
    copied.value = false
  }, 200)
}
</script>

<template>
  <UiDialog
    :open="open"
    title="Register Runner"
    description="Register a new runner instance. You'll receive an API token — save it, it's shown only once."
    @update:open="(v) => (v ? (open = true) : handleClose())"
  >
    <template #trigger>
      <UiButton @click="open = true">
        Register Runner
      </UiButton>
    </template>

    <!-- Before creation: Name input form -->
    <form v-if="!createdToken" class="flex flex-col gap-4" @submit.prevent="handleSubmit">
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Name</label>
        <UiInput
          v-model="name"
          placeholder="e.g. dev-runner-01"
        />
        <p class="text-xs text-muted-fg mt-1">Optional. A friendly name for this runner.</p>
      </div>

      <div class="flex justify-end gap-2">
        <UiButton variant="outline" type="button" @click="handleClose">
          Cancel
        </UiButton>
        <UiButton type="submit" :disabled="submitting">
          {{ submitting ? 'Registering…' : 'Register' }}
        </UiButton>
      </div>
    </form>

    <!-- After creation: Show token -->
    <div v-else class="flex flex-col gap-4">
      <div
        class="p-4 rounded-[var(--radius-md)] bg-warning-muted border border-warning/30"
      >
        <p class="text-sm font-medium text-fg mb-2">⚠️ Save this API token now</p>
        <p class="text-xs text-muted-fg mb-3">
          This token will not be shown again. Store it securely.
        </p>
        <div class="flex items-center gap-2">
          <code
            class="flex-1 text-xs bg-surface px-3 py-2 rounded-[var(--radius-sm)] border border-border font-mono break-all select-all"
          >
            {{ createdToken }}
          </code>
          <UiButton variant="outline" size="icon-sm" @click="handleCopyToken">
            <component :is="copied ? Check : Copy" :size="14" />
          </UiButton>
        </div>
      </div>

      <div class="flex justify-end">
        <UiButton @click="handleClose">Done</UiButton>
      </div>
    </div>
  </UiDialog>
</template>
