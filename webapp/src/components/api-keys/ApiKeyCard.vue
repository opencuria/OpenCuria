<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import type { APIKey } from '@/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Switch } from '@/components/ui/switch'
import SettingsRow from '@/components/settings/SettingsRow.vue'
import {
  KeyRound,
  Clock,
  Zap,
  Trash2,
  CheckCircle2,
  XCircle,
  Shield,
  ChevronDown,
} from '@lucide/vue'
import { formatDate, formatRelativeTime } from '@/lib/utils'
import { useApiKeyStore } from '@/stores/apiKeys'
import { useAutosave } from '@/composables/useAutosave'
import AutosaveStatus from '@/components/settings/AutosaveStatus.vue'

const props = defineProps<{
  apiKey: APIKey
}>()

const emit = defineEmits<{
  revoke: [apiKey: APIKey]
}>()

const apiKeyStore = useApiKeyStore()

const editingPermissions = ref(false)
const localPermissions = ref<string[]>([...props.apiKey.permissions])
const savedPermissions = ref([...props.apiKey.permissions])

onMounted(() => {
  if (apiKeyStore.availablePermissions.length === 0) {
    apiKeyStore.fetchAvailablePermissions()
  }
})

watch(
  () => props.apiKey.permissions,
  (next) => {
    if (!autosave.dirty && !autosave.saving) {
      localPermissions.value = [...next]
      savedPermissions.value = [...next]
    }
  },
)

const fullAccess = computed(() => localPermissions.value.length === 0)

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
    if (!localPermissions.value.includes(value)) {
      localPermissions.value = [...localPermissions.value, value]
    }
    return
  }
  // An empty list grants full access. Require the explicit switch for that change.
  if (localPermissions.value.length === 1 && localPermissions.value[0] === value) return
  localPermissions.value = localPermissions.value.filter((p) => p !== value)
}

function toggleFullAccess(checked: boolean | 'indeterminate'): void {
  if (checked === true) {
    localPermissions.value = []
    return
  }
  localPermissions.value = apiKeyStore.availablePermissions.map((p) => p.value)
}

const autosave = useAutosave({
  source: () => localPermissions.value,
  dirty: () =>
    JSON.stringify([...localPermissions.value].sort()) !==
    JSON.stringify([...savedPermissions.value].sort()),
  enabled: () => props.apiKey.is_active,
  scope: () => props.apiKey.id,
  errorMessage: 'Failed to update permissions',
  save: async (isCurrent) => {
    const permissions = [...localPermissions.value]
    if (!(await apiKeyStore.updateKeyPermissions(props.apiKey.id, permissions, { silent: true })))
      throw new Error('Failed to update permissions')
    if (isCurrent()) savedPermissions.value = permissions
  },
})

async function toggleEditor(): Promise<void> {
  if (!editingPermissions.value || (await autosave.flush()))
    editingPermissions.value = !editingPermissions.value
}
</script>

<template>
  <SettingsRow :icon-class="apiKey.is_active ? 'bg-primary/10 text-primary' : undefined">
    <template #icon>
      <KeyRound :size="16" />
    </template>
    <div class="min-w-0 space-y-1.5">
      <div class="flex flex-wrap items-center gap-2">
        <h3 class="text-sm font-medium text-foreground">{{ apiKey.name }}</h3>
        <Badge variant="secondary">
          <component :is="apiKey.is_active ? CheckCircle2 : XCircle" :size="11" />
          {{ apiKey.is_active ? 'Active' : 'Revoked' }}
        </Badge>
        <Badge variant="outline">
          {{ apiKey.expires_at ? `Expires ${formatDate(apiKey.expires_at)}` : 'Never expires' }}
        </Badge>
        <Badge :variant="apiKey.permissions.length === 0 ? 'default' : 'outline'">
          <Shield :size="10" />
          {{
            apiKey.permissions.length === 0
              ? 'Full access'
              : `${apiKey.permissions.length} permission${apiKey.permissions.length !== 1 ? 's' : ''}`
          }}
        </Badge>
      </div>
      <p class="font-mono text-xs text-muted-foreground">{{ apiKey.key_prefix }}…</p>
      <div class="flex flex-col gap-1">
        <p class="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Clock :size="12" />
          Created {{ formatRelativeTime(apiKey.created_at) }}
        </p>
        <p class="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Zap :size="12" />
          {{ apiKey.last_used_at ? `Last used ${formatRelativeTime(apiKey.last_used_at)}` : 'Never used' }}
        </p>
      </div>
      <div
        v-if="apiKey.is_active && !editingPermissions && apiKey.permissions.length > 0"
        class="flex flex-wrap gap-1"
      >
        <span
          v-for="p in apiKey.permissions.slice(0, 4)"
          :key="p"
          class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground"
        >{{ p }}</span>
        <span v-if="apiKey.permissions.length > 4" class="text-xs text-muted-foreground">
          +{{ apiKey.permissions.length - 4 }} more
        </span>
      </div>
    </div>
    <template #actions>
      <Button
        v-if="apiKey.is_active"
        variant="ghost"
        size="icon-sm"
        class="text-destructive hover:text-destructive"
        title="Revoke key"
        @click="emit('revoke', apiKey)"
      >
        <Trash2 />
      </Button>
    </template>
    <template v-if="apiKey.is_active" #detail>
      <button
        type="button"
        class="flex w-full cursor-pointer items-center justify-between border-t border-border py-2 text-xs text-muted-foreground transition-colors hover:text-foreground"
        @click="toggleEditor"
      >
        <span class="flex items-center gap-1.5">
          <Shield :size="12" />
          Edit permissions
        </span>
        <ChevronDown
          :size="12"
          class="transition-transform"
          :class="editingPermissions ? 'rotate-180' : undefined"
        />
      </button>

      <div v-if="editingPermissions" class="mt-2 space-y-3">
        <div class="flex items-center justify-between gap-3">
          <span class="text-xs text-muted-foreground">Full access</span>
          <Switch :model-value="fullAccess" @update:model-value="toggleFullAccess" />
        </div>

        <div
          v-if="fullAccess"
          class="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
        >
          No restrictions — this key can access all operations.
        </div>

        <div v-else class="max-h-56 space-y-2 overflow-y-auto pr-1">
          <template v-for="(perms, group) in permissionGroups" :key="group">
            <div>
              <p class="mb-1 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                {{ group }}
              </p>
              <div class="space-y-0.5">
                <label
                  v-for="perm in perms"
                  :key="perm.value"
                  class="flex cursor-pointer items-center gap-2 rounded-md border px-2 py-1.5 transition-colors"
                  :class="
                    localPermissions.includes(perm.value)
                      ? 'border-primary/40 bg-primary/5'
                      : 'border-border bg-background hover:border-border'
                  "
                >
                  <Checkbox
                    :model-value="localPermissions.includes(perm.value)"
                    :disabled="localPermissions.length === 1 && localPermissions[0] === perm.value"
                    @update:model-value="setPermission(perm.value, $event)"
                  />
                  <span class="min-w-0 font-mono text-xs text-foreground">{{ perm.value }}</span>
                </label>
              </div>
            </div>
          </template>
        </div>

        <AutosaveStatus :state="autosave" />
      </div>
    </template>
  </SettingsRow>
</template>
