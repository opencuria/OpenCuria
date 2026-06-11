<script setup lang="ts">
import { ref } from 'vue'
import { useRunnerStore } from '@/stores/runners'
import { Copy, Check } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'

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
  setTimeout(() => {
    name.value = ''
    createdToken.value = null
    copied.value = false
  }, 200)
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => (v ? (open = true) : handleClose())">
    <DialogTrigger as-child>
      <Button @click="open = true">
        Register Runner
      </Button>
    </DialogTrigger>

    <DialogContent>
      <DialogHeader>
        <DialogTitle>Register Runner</DialogTitle>
        <DialogDescription>
          Register a new runner instance. You'll receive an API token — save it, it's shown only once.
        </DialogDescription>
      </DialogHeader>

      <form v-if="!createdToken" class="flex flex-col gap-4" @submit.prevent="handleSubmit">
        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
          <Input
            v-model="name"
            placeholder="e.g. dev-runner-01"
          />
          <p class="text-xs text-muted-foreground mt-1">Optional. A friendly name for this runner.</p>
        </div>

        <div class="flex justify-end gap-2">
          <Button variant="outline" type="button" @click="handleClose">
            Cancel
          </Button>
          <Button type="submit" :disabled="submitting">
            {{ submitting ? 'Registering…' : 'Register' }}
          </Button>
        </div>
      </form>

      <div v-else class="flex flex-col gap-4">
        <div class="p-4 rounded-[var(--radius-md)] bg-amber-500/10 border border-amber-500/30">
          <p class="text-sm font-medium text-foreground mb-2">⚠️ Save this API token now</p>
          <p class="text-xs text-muted-foreground mb-3">
            This token will not be shown again. Store it securely.
          </p>
          <div class="flex items-center gap-2">
            <code
              class="flex-1 text-xs bg-card px-3 py-2 rounded-[var(--radius-sm)] border border-border font-mono break-all select-all"
            >
              {{ createdToken }}
            </code>
            <Button variant="outline" size="icon-sm" @click="handleCopyToken">
              <component :is="copied ? Check : Copy" :size="14" />
            </Button>
          </div>
        </div>

        <div class="flex justify-end">
          <Button @click="handleClose">Done</Button>
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>
