<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { UiDialog, UiInput, UiButton } from '@/components/ui'
import { useApiKeyStore } from '@/stores/apiKeys'
import type { APIKeyCreatedOut } from '@/types'
import { Copy, CheckCheck, AlertTriangle, KeyRound, Shield, ShieldOff } from 'lucide-vue-next'

const apiKeyStore = useApiKeyStore()

const open = ref(false)
const name = ref('')
const expiresAt = ref('')
const selectedPermissions = ref<string[]>([])
const fullAccess = ref(true)
const submitting = ref(false)
const createdKey = ref<APIKeyCreatedOut | null>(null)
const copied = ref(false)

onMounted(() => {
  apiKeyStore.fetchAvailablePermissions()
})

const isValid = computed(() => name.value.trim().length > 0)

const permissionGroups = computed(() => {
  const groups: Record<string, typeof apiKeyStore.availablePermissions> = {}
  for (const p of apiKeyStore.availablePermissions) {
    const groupPermissions = groups[p.group] ?? (groups[p.group] = [])
    groupPermissions.push(p)
  }
  return groups
})

function togglePermission(value: string) {
  if (selectedPermissions.value.includes(value)) {
    selectedPermissions.value = selectedPermissions.value.filter((p) => p !== value)
  } else {
    selectedPermissions.value = [...selectedPermissions.value, value]
  }
}

function toggleFullAccess() {
  fullAccess.value = !fullAccess.value
  if (fullAccess.value) {
    selectedPermissions.value = []
  }
}

async function handleSubmit(): Promise<void> {
  if (!isValid.value) return

  submitting.value = true
  const result = await apiKeyStore.createKey({
    name: name.value.trim(),
    expires_at: expiresAt.value || null,
    permissions: fullAccess.value ? [] : selectedPermissions.value,
  })
  submitting.value = false

  if (result) {
    createdKey.value = result
  }
}

async function copyToken(): Promise<void> {
  if (!createdKey.value) return
  await navigator.clipboard.writeText(createdKey.value.key)
  copied.value = true
  setTimeout(() => (copied.value = false), 2000)
}

function handleClose(): void {
  open.value = false
  setTimeout(() => {
    name.value = ''
    expiresAt.value = ''
    selectedPermissions.value = []
    fullAccess.value = true
    createdKey.value = null
    copied.value = false
  }, 200)
}
</script>

<template>
  <UiDialog
    :open="open"
    title="Create API Key"
    description="Generate a long-lived key for external integrations like n8n or Zapier."
    @update:open="(v) => (v ? (open = true) : handleClose())"
  >
    <template #trigger>
      <UiButton @click="open = true">
        <KeyRound :size="15" class="mr-1.5" />
        New API Key
      </UiButton>
    </template>

    <!-- Step 1: Form -->
    <form v-if="!createdKey" class="flex flex-col gap-4" @submit.prevent="handleSubmit">
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Name</label>
        <UiInput
          v-model="name"
          placeholder="e.g. n8n prod, Zapier integration"
          autofocus
        />
        <p class="text-xs text-muted-fg mt-1">A label to help you identify this key later.</p>
      </div>

      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">
          Expiry
          <span class="text-muted-fg font-normal">(optional)</span>
        </label>
        <UiInput
          v-model="expiresAt"
          type="datetime-local"
        />
        <p class="text-xs text-muted-fg mt-1">Leave empty for a key that never expires.</p>
      </div>

      <!-- Permissions -->
      <div>
        <div class="flex items-center justify-between mb-2">
          <label class="text-sm font-medium text-fg">Permissions</label>
          <button
            type="button"
            class="flex items-center gap-1.5 text-xs px-2 py-1 rounded-[var(--radius-sm)] border transition-colors cursor-pointer"
            :class="fullAccess
              ? 'border-primary/40 bg-primary/10 text-primary'
              : 'border-border bg-bg text-muted-fg hover:text-fg'"
            @click="toggleFullAccess"
          >
            <component :is="fullAccess ? Shield : ShieldOff" :size="12" />
            {{ fullAccess ? 'Full access' : 'Restricted' }}
          </button>
        </div>

        <div v-if="fullAccess" class="rounded-[var(--radius-md)] border border-border bg-bg px-3.5 py-3 text-xs text-muted-fg">
          This key will have access to all operations. Toggle to restrict permissions.
        </div>

        <div v-else class="space-y-3 max-h-64 overflow-y-auto pr-1">
          <template v-for="(perms, group) in permissionGroups" :key="group">
            <div>
              <p class="text-xs font-medium text-muted-fg uppercase tracking-wide mb-1.5">{{ group }}</p>
              <div class="space-y-1">
                <label
                  v-for="perm in perms"
                  :key="perm.value"
                  class="flex items-start gap-2.5 p-2 rounded-[var(--radius-sm)] border cursor-pointer transition-colors"
                  :class="selectedPermissions.includes(perm.value)
                    ? 'border-primary/40 bg-primary/5'
                    : 'border-border bg-bg hover:border-border-hover'"
                >
                  <input
                    type="checkbox"
                    :checked="selectedPermissions.includes(perm.value)"
                    class="mt-0.5 shrink-0 accent-primary cursor-pointer"
                    @change="togglePermission(perm.value)"
                  />
                  <div class="min-w-0">
                    <p class="text-xs font-medium text-fg font-mono">{{ perm.value }}</p>
                    <p class="text-xs text-muted-fg mt-0.5">{{ perm.description }}</p>
                  </div>
                </label>
              </div>
            </div>
          </template>
        </div>
      </div>

      <div class="flex justify-end gap-2 pt-2">
        <UiButton variant="outline" type="button" @click="handleClose">Cancel</UiButton>
        <UiButton type="submit" :disabled="!isValid || submitting">
          {{ submitting ? 'Creating…' : 'Create Key' }}
        </UiButton>
      </div>
    </form>

    <!-- Step 2: Token reveal (shown once) -->
    <div v-else class="flex flex-col gap-4">
      <!-- Warning banner -->
      <div class="flex items-start gap-2.5 rounded-[var(--radius-md)] border border-warning/40 bg-warning-muted px-3.5 py-3 text-sm text-warning">
        <AlertTriangle :size="16" class="mt-0.5 shrink-0" />
        <div>
          <p class="font-medium">Copy your key now</p>
          <p class="text-warning/80 text-xs mt-0.5">This token will not be shown again. OpenCuria only stores a hash.</p>
        </div>
      </div>

      <!-- Token display -->
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Your API Key</label>
        <div class="flex gap-2">
          <div
            class="flex-1 min-w-0 rounded-[var(--radius-md)] border border-border bg-bg px-3 py-2 font-mono text-xs text-fg break-all select-all"
          >
            {{ createdKey.key }}
          </div>
          <button
            class="flex items-center justify-center w-9 h-9 shrink-0 rounded-[var(--radius-md)] border border-border bg-surface hover:bg-surface-hover transition-colors cursor-pointer text-muted-fg hover:text-fg"
            :title="copied ? 'Copied!' : 'Copy to clipboard'"
            @click="copyToken"
          >
            <component :is="copied ? CheckCheck : Copy" :size="15" :class="copied ? 'text-success' : ''" />
          </button>
        </div>
      </div>

      <!-- Permissions summary -->
      <div class="rounded-[var(--radius-md)] border border-border bg-bg px-3.5 py-3 text-xs space-y-1.5">
        <p class="font-medium text-fg text-xs flex items-center gap-1.5">
          <Shield :size="12" class="text-primary" />
          {{ createdKey.permissions.length > 0 ? 'Permissions granted' : 'Full access (no restrictions)' }}
        </p>
        <div v-if="createdKey.permissions.length > 0" class="flex flex-wrap gap-1">
          <span
            v-for="p in createdKey.permissions"
            :key="p"
            class="font-mono bg-muted px-1.5 py-0.5 rounded text-muted-fg"
          >{{ p }}</span>
        </div>
      </div>

      <!-- Usage hint -->
      <div class="rounded-[var(--radius-md)] border border-border bg-bg px-3.5 py-3 text-xs text-muted-fg space-y-1.5">
        <p class="font-medium text-fg text-xs">How to use (REST API)</p>
        <p><span class="font-mono bg-muted px-1 py-0.5 rounded">Authorization: Bearer {{ createdKey.key_prefix }}…</span></p>
        <p>or</p>
        <p><span class="font-mono bg-muted px-1 py-0.5 rounded">X-API-Key: {{ createdKey.key_prefix }}…</span></p>
        <p class="mt-2 font-medium text-fg text-xs">MCP endpoint (SSE)</p>
        <p><span class="font-mono bg-muted px-1 py-0.5 rounded">/mcp/sse</span> — requires <span class="font-mono">mcp:access</span> permission</p>
      </div>

      <div class="flex justify-end pt-1">
        <UiButton @click="handleClose">Done</UiButton>
      </div>
    </div>
  </UiDialog>
</template>
