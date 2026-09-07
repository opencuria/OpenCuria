<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApiKeyStore } from '@/stores/apiKeys'
import type { APIKeyCreatedOut } from '@/types'
import { Copy, CheckCheck, AlertTriangle, KeyRound, Shield } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'

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

function setPermission(value: string, checked: boolean | 'indeterminate'): void {
  if (checked === true) {
    if (!selectedPermissions.value.includes(value)) {
      selectedPermissions.value = [...selectedPermissions.value, value]
    }
    return
  }
  selectedPermissions.value = selectedPermissions.value.filter((p) => p !== value)
}

function setFullAccess(checked: boolean | 'indeterminate'): void {
  fullAccess.value = checked === true
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
  <Dialog :open="open" @update:open="(v) => (v ? (open = true) : handleClose())">
    <DialogTrigger as-child>
      <Button size="sm" @click="open = true">
        <KeyRound />
        New API Key
      </Button>
    </DialogTrigger>

    <DialogContent>
      <DialogHeader>
        <DialogTitle>Create API Key</DialogTitle>
        <DialogDescription>
          Generate a long-lived key for external integrations like n8n or Zapier.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
      <form v-if="!createdKey" id="create-api-key-form" class="flex flex-col gap-4" @submit.prevent="handleSubmit">
        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
          <Input
            v-model="name"
            placeholder="e.g. n8n prod, Zapier integration"
            autofocus
          />
          <p class="text-xs text-muted-foreground mt-1">A label to help you identify this key later.</p>
        </div>

        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">
            Expiry
            <span class="text-muted-foreground font-normal">(optional)</span>
          </label>
          <Input
            v-model="expiresAt"
            type="datetime-local"
          />
          <p class="text-xs text-muted-foreground mt-1">Leave empty for a key that never expires.</p>
        </div>

        <div>
          <div class="mb-2 flex items-center justify-between gap-3">
            <Label>Permissions</Label>
            <div class="flex items-center gap-2">
              <span class="text-xs text-muted-foreground">{{ fullAccess ? 'Full access' : 'Restricted' }}</span>
              <Switch :model-value="fullAccess" @update:model-value="setFullAccess" />
            </div>
          </div>

          <div
            v-if="fullAccess"
            class="rounded-md border border-border bg-muted/40 px-3.5 py-3 text-xs text-muted-foreground"
          >
            This key will have access to all operations. Toggle to restrict permissions.
          </div>

          <div v-else class="max-h-64 space-y-3 overflow-y-auto pr-1">
            <template v-for="(perms, group) in permissionGroups" :key="group">
              <div>
                <p class="mb-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">{{ group }}</p>
                <div class="space-y-1">
                  <label
                    v-for="perm in perms"
                    :key="perm.value"
                    class="flex cursor-pointer items-start gap-2.5 rounded-md border p-2 transition-colors"
                    :class="
                      selectedPermissions.includes(perm.value)
                        ? 'border-primary/40 bg-primary/5'
                        : 'border-border bg-background hover:border-border'
                    "
                  >
                    <Checkbox
                      class="mt-0.5"
                      :model-value="selectedPermissions.includes(perm.value)"
                      @update:model-value="setPermission(perm.value, $event)"
                    />
                    <div class="min-w-0">
                      <p class="font-mono text-xs font-medium text-foreground">{{ perm.value }}</p>
                      <p class="mt-0.5 text-xs text-muted-foreground">{{ perm.description }}</p>
                    </div>
                  </label>
                </div>
              </div>
            </template>
          </div>
        </div>

      </form>

      <div v-else class="flex flex-col gap-4">
        <div class="flex items-start gap-2.5 rounded-md border border-warning/40 bg-warning-muted px-3.5 py-3 text-sm text-warning">
          <AlertTriangle :size="16" class="mt-0.5 shrink-0" />
          <div>
            <p class="font-medium">Copy your key now</p>
            <p class="mt-0.5 text-xs text-warning/80">
              This token will not be shown again. OpenCuria only stores a hash.
            </p>
          </div>
        </div>

        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">Your API Key</label>
          <div class="flex gap-2">
            <div
              class="min-w-0 flex-1 break-all rounded-md border border-border bg-background px-3 py-2 font-mono text-xs text-foreground select-all"
            >
              {{ createdKey.key }}
            </div>
            <Button
              variant="outline"
              size="icon"
              :title="copied ? 'Copied!' : 'Copy to clipboard'"
              @click="copyToken"
            >
              <component :is="copied ? CheckCheck : Copy" :class="copied ? 'text-success' : ''" />
            </Button>
          </div>
        </div>

        <div class="space-y-1.5 rounded-md border border-border bg-background px-3.5 py-3 text-xs">
          <p class="flex items-center gap-1.5 text-xs font-medium text-foreground">
            <Shield :size="12" class="text-primary" />
            {{ createdKey.permissions.length > 0 ? 'Permissions granted' : 'Full access (no restrictions)' }}
          </p>
          <div v-if="createdKey.permissions.length > 0" class="flex flex-wrap gap-1">
            <span
              v-for="p in createdKey.permissions"
              :key="p"
              class="rounded bg-muted px-1.5 py-0.5 font-mono text-muted-foreground"
            >{{ p }}</span>
          </div>
        </div>

        <div class="space-y-1.5 rounded-md border border-border bg-background px-3.5 py-3 text-xs text-muted-foreground">
          <p class="text-xs font-medium text-foreground">How to use (REST API)</p>
          <p><span class="rounded bg-muted px-1 py-0.5 font-mono">Authorization: Bearer {{ createdKey.key_prefix }}…</span></p>
          <p>or</p>
          <p><span class="rounded bg-muted px-1 py-0.5 font-mono">X-API-Key: {{ createdKey.key_prefix }}…</span></p>
          <p class="mt-2 text-xs font-medium text-foreground">MCP endpoint (SSE)</p>
          <p><span class="rounded bg-muted px-1 py-0.5 font-mono">/mcp/sse</span> — requires <span class="font-mono">mcp:access</span> permission</p>
        </div>

      </div>
      </DialogBody>

      <DialogFooter v-if="!createdKey">
        <Button variant="outline" type="button" @click="handleClose">Cancel</Button>
        <Button type="submit" form="create-api-key-form" :disabled="!isValid || submitting">
          {{ submitting ? 'Creating…' : 'Create Key' }}
        </Button>
      </DialogFooter>
      <DialogFooter v-else>
        <Button @click="handleClose">Done</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
