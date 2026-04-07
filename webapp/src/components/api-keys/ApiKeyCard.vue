<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import type { APIKey } from '@/types'
import { UiCard, UiCardContent, UiBadge, UiButton } from '@/components/ui'
import { KeyRound, Clock, Zap, Trash2, CheckCircle2, XCircle, Shield, ShieldOff, ChevronDown, ChevronUp, Check } from 'lucide-vue-next'
import { formatDate, formatRelativeTime } from '@/lib/utils'
import { useApiKeyStore } from '@/stores/apiKeys'

const props = defineProps<{
  apiKey: APIKey
}>()

const emit = defineEmits<{
  revoke: [apiKey: APIKey]
}>()

const apiKeyStore = useApiKeyStore()

const editingPermissions = ref(false)
const localPermissions = ref<string[]>([...props.apiKey.permissions])
const savingPermissions = ref(false)

onMounted(() => {
  if (apiKeyStore.availablePermissions.length === 0) {
    apiKeyStore.fetchAvailablePermissions()
  }
})

const fullAccess = computed(() => localPermissions.value.length === 0)

const permissionGroups = computed(() => {
  const groups: Record<string, typeof apiKeyStore.availablePermissions> = {}
  for (const p of apiKeyStore.availablePermissions) {
    const groupPermissions = groups[p.group] ?? (groups[p.group] = [])
    groupPermissions.push(p)
  }
  return groups
})

function togglePermission(value: string) {
  if (localPermissions.value.includes(value)) {
    localPermissions.value = localPermissions.value.filter((p) => p !== value)
  } else {
    localPermissions.value = [...localPermissions.value, value]
  }
}

function toggleFullAccess() {
  if (localPermissions.value.length > 0) {
    localPermissions.value = []
  } else {
    // Select all permissions
    localPermissions.value = apiKeyStore.availablePermissions.map((p) => p.value)
  }
}

function cancelEdit() {
  localPermissions.value = [...props.apiKey.permissions]
  editingPermissions.value = false
}

async function savePermissions() {
  savingPermissions.value = true
  const ok = await apiKeyStore.updateKeyPermissions(props.apiKey.id, localPermissions.value)
  savingPermissions.value = false
  if (ok) {
    editingPermissions.value = false
  }
}
</script>

<template>
  <UiCard class="hover:border-border-hover transition-colors duration-150">
    <UiCardContent>
      <div class="flex items-start justify-between mb-3">
        <div class="flex items-center gap-3">
          <div
            class="flex items-center justify-center w-10 h-10 rounded-[var(--radius-md)] shrink-0"
            :class="apiKey.is_active ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-fg'"
          >
            <KeyRound :size="18" />
          </div>
          <div class="min-w-0">
            <h3 class="font-medium text-fg text-sm truncate">{{ apiKey.name }}</h3>
            <p class="text-xs text-muted-fg font-mono mt-0.5">{{ apiKey.key_prefix }}…</p>
          </div>
        </div>

        <button
          v-if="apiKey.is_active"
          class="p-1.5 rounded-[var(--radius-sm)] text-muted-fg hover:text-error hover:bg-error-muted transition-colors cursor-pointer shrink-0 ml-2"
          title="Revoke key"
          @click="emit('revoke', apiKey)"
        >
          <Trash2 :size="14" />
        </button>
      </div>

      <!-- Status badges -->
      <div class="flex flex-wrap gap-1.5 mb-3">
        <UiBadge :variant="apiKey.is_active ? 'success' : 'muted'">
          <component :is="apiKey.is_active ? CheckCircle2 : XCircle" :size="11" class="mr-1" />
          {{ apiKey.is_active ? 'Active' : 'Revoked' }}
        </UiBadge>
        <UiBadge variant="outline">
          {{ apiKey.expires_at ? `Expires ${formatDate(apiKey.expires_at)}` : 'Never expires' }}
        </UiBadge>
        <UiBadge :variant="apiKey.permissions.length === 0 ? 'default' : 'outline'">
          <Shield :size="10" class="mr-1" />
          {{ apiKey.permissions.length === 0 ? 'Full access' : `${apiKey.permissions.length} permission${apiKey.permissions.length !== 1 ? 's' : ''}` }}
        </UiBadge>
      </div>

      <!-- Timestamps -->
      <div class="flex flex-col gap-1 mb-3">
        <div class="flex items-center gap-1.5 text-xs text-muted-fg">
          <Clock :size="12" />
          <span>Created {{ formatRelativeTime(apiKey.created_at) }}</span>
        </div>
        <div class="flex items-center gap-1.5 text-xs text-muted-fg">
          <Zap :size="12" />
          <span>
            {{ apiKey.last_used_at ? `Last used ${formatRelativeTime(apiKey.last_used_at)}` : 'Never used' }}
          </span>
        </div>
      </div>

      <!-- Permissions section -->
      <div v-if="apiKey.is_active">
        <!-- Toggle permissions editor -->
        <button
          class="w-full flex items-center justify-between text-xs text-muted-fg hover:text-fg transition-colors py-1.5 border-t border-border cursor-pointer"
          @click="editingPermissions = !editingPermissions"
        >
          <span class="flex items-center gap-1.5">
            <Shield :size="12" />
            Edit permissions
          </span>
          <component :is="editingPermissions ? ChevronUp : ChevronDown" :size="12" />
        </button>

        <div v-if="editingPermissions" class="mt-2 space-y-3">
          <!-- Full access toggle -->
          <div class="flex items-center justify-between">
            <span class="text-xs text-muted-fg">Access level</span>
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

          <div v-if="fullAccess" class="rounded-[var(--radius-md)] border border-border bg-bg px-3 py-2 text-xs text-muted-fg">
            No restrictions — key can access all operations.
          </div>

          <div v-else class="space-y-2 max-h-56 overflow-y-auto pr-1">
            <template v-for="(perms, group) in permissionGroups" :key="group">
              <div>
                <p class="text-xs font-medium text-muted-fg uppercase tracking-wide mb-1">{{ group }}</p>
                <div class="space-y-0.5">
                  <label
                    v-for="perm in perms"
                    :key="perm.value"
                    class="flex items-center gap-2 p-1.5 rounded-[var(--radius-sm)] border cursor-pointer transition-colors"
                    :class="localPermissions.includes(perm.value)
                      ? 'border-primary/40 bg-primary/5'
                      : 'border-border bg-bg hover:border-border-hover'"
                  >
                    <input
                      type="checkbox"
                      :checked="localPermissions.includes(perm.value)"
                      class="shrink-0 accent-primary cursor-pointer"
                      @change="togglePermission(perm.value)"
                    />
                    <span class="text-xs font-mono text-fg truncate">{{ perm.value }}</span>
                  </label>
                </div>
              </div>
            </template>
          </div>

          <!-- Save/Cancel buttons -->
          <div class="flex gap-2 justify-end pt-1">
            <UiButton size="sm" variant="outline" @click="cancelEdit">Cancel</UiButton>
            <UiButton size="sm" :disabled="savingPermissions" @click="savePermissions">
              <Check :size="12" class="mr-1" />
              {{ savingPermissions ? 'Saving…' : 'Save' }}
            </UiButton>
          </div>
        </div>

        <!-- Current permissions pills (compact, when not editing) -->
        <div v-else-if="apiKey.permissions.length > 0" class="mt-1.5 flex flex-wrap gap-1">
          <span
            v-for="p in apiKey.permissions.slice(0, 4)"
            :key="p"
            class="font-mono text-[10px] bg-muted px-1.5 py-0.5 rounded text-muted-fg"
          >{{ p }}</span>
          <span v-if="apiKey.permissions.length > 4" class="text-[10px] text-muted-fg">+{{ apiKey.permissions.length - 4 }} more</span>
        </div>
      </div>
    </UiCardContent>
  </UiCard>
</template>
