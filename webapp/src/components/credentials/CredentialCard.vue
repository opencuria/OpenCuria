<script setup lang="ts">
import { computed } from 'vue'
import type { Credential } from '@/types'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { useAuthStore } from '@/stores/auth'
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
</script>

<template>
  <Card class="hover:border-border transition-colors duration-150">
    <CardContent>
      <div class="flex items-start justify-between mb-3">
        <div class="flex items-center gap-3">
          <div
            class="flex items-center justify-center w-10 h-10 rounded-[var(--radius-md)] bg-primary/10 text-primary"
          >
            <KeyRound :size="18" />
          </div>
          <div>
            <h3 class="font-medium text-foreground text-sm">{{ credential.name }}</h3>
            <p class="text-xs text-muted-foreground">{{ credential.service_name }}</p>
          </div>
        </div>

        <div class="flex items-center gap-1">
          <button
            v-if="credential.has_public_key"
            class="p-1.5 rounded-[var(--radius-sm)] text-muted-foreground hover:text-foreground hover:bg-muted transition-colors cursor-pointer"
            title="View public key"
            @click="emit('viewPublicKey', credential)"
          >
            <Eye :size="14" />
          </button>
          <template v-if="canEdit">
            <button
              class="p-1.5 rounded-[var(--radius-sm)] text-muted-foreground hover:text-foreground hover:bg-muted transition-colors cursor-pointer"
              title="Edit credential"
              @click="emit('edit', credential)"
            >
              <Pencil :size="14" />
            </button>
            <button
              class="p-1.5 rounded-[var(--radius-sm)] text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors cursor-pointer"
              title="Delete credential"
              @click="emit('delete', credential)"
            >
              <Trash2 :size="14" />
            </button>
          </template>
        </div>
      </div>

      <div class="mb-3">
        <div class="flex flex-wrap gap-1.5">
          <Badge :variant="credential.scope === 'organization' ? 'default' : 'secondary'">
            {{ credential.scope === 'organization' ? 'Organization' : 'Personal' }}
          </Badge>
          <Badge variant="outline">
            {{
              credential.credential_type === 'ssh_key'
                ? 'SSH Key'
                : credential.credential_type === 'file'
                  ? 'File'
                  : 'ENV'
            }}
          </Badge>
          <Badge v-if="credential.env_var_name" variant="secondary">{{ credential.env_var_name }}</Badge>
          <Badge v-if="credential.target_path" variant="secondary">{{ credential.target_path }}</Badge>
        </div>
      </div>

      <div class="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Clock :size="12" />
        <span>Created {{ formatRelativeTime(credential.created_at) }}</span>
      </div>
    </CardContent>
  </Card>
</template>
