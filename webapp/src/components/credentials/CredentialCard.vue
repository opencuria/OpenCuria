<script setup lang="ts">
import { computed } from 'vue'
import type { Credential } from '@/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useAuthStore } from '@/stores/auth'
import SettingsRow from '@/components/settings/SettingsRow.vue'
import { KeyRound, Clock, Eye, Pencil, Trash2 } from '@lucide/vue'
import { formatRelativeTime } from '@/lib/utils'

const props = defineProps<{
  credential: Credential
}>()

const emit = defineEmits<{
  edit: [credential: Credential]
  delete: [credential: Credential]
  viewPublicKey: [credential: Credential]
}>()

const authStore = useAuthStore()

const canEdit = computed(() => {
  if (props.credential.scope === 'personal') {
    return authStore.user?.id === props.credential.created_by_id
  }
  return authStore.isAdmin
})

const typeLabel = computed(() => {
  if (props.credential.credential_type === 'ssh_key') return 'SSH Key'
  if (props.credential.credential_type === 'file') return 'File'
  return 'ENV'
})
</script>

<template>
  <SettingsRow>
    <template #icon>
      <KeyRound :size="16" />
    </template>
    <div class="min-w-0 space-y-1">
      <div class="flex flex-wrap items-center gap-2">
        <h3 class="text-sm font-medium text-foreground">{{ credential.name }}</h3>
        <Badge :variant="credential.scope === 'organization' ? 'default' : 'secondary'">
          {{ credential.scope === 'organization' ? 'Organization' : 'Personal' }}
        </Badge>
        <Badge variant="outline">{{ typeLabel }}</Badge>
      </div>
      <p class="text-sm text-muted-foreground">{{ credential.service_name }}</p>
      <p v-if="credential.env_var_name" class="font-mono text-xs text-muted-foreground">
        {{ credential.env_var_name }}
      </p>
      <p v-if="credential.target_path" class="font-mono text-xs text-muted-foreground break-all">
        {{ credential.target_path }}
      </p>
      <p class="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Clock :size="12" />
        Created {{ formatRelativeTime(credential.created_at) }}
      </p>
    </div>
    <template #actions>
      <Button
        v-if="credential.has_public_key"
        variant="ghost"
        size="icon-sm"
        title="View public key"
        @click="emit('viewPublicKey', credential)"
      >
        <Eye />
      </Button>
      <template v-if="canEdit">
        <Button
          variant="ghost"
          size="icon-sm"
          title="Edit credential"
          @click="emit('edit', credential)"
        >
          <Pencil />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          class="text-destructive hover:text-destructive"
          title="Delete credential"
          @click="emit('delete', credential)"
        >
          <Trash2 />
        </Button>
      </template>
    </template>
  </SettingsRow>
</template>
