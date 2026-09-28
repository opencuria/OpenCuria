<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Button } from '@/components/ui/button'
import { useAuthStore } from '@/stores/auth'
import { usePluginStore } from '@/stores/plugins'
import type { Plugin, PluginMcpServer } from '@/types'

const props = defineProps<{ plugin: Plugin; server: PluginMcpServer }>()
const authStore = useAuthStore()
const pluginStore = usePluginStore()
const busy = ref<string | null>(null)

const requirement = computed(() => props.plugin.credential_requirements.find(
  (item) => item.key === props.server.oauth_requirement_key,
))
const status = computed(() => pluginStore.getMcpOAuthStatus(props.plugin.id, props.server.id))
const isLoading = computed(() => pluginStore.mcpOAuthLoading[`${props.plugin.id}:${props.server.id}`] ?? false)

onMounted(() => {
  void pluginStore.fetchMcpOAuthStatus(props.plugin.id, props.server.id)
})

function connected(scope: 'personal' | 'organization'): boolean {
  return status.value?.[scope].connected ?? false
}

async function connect(scope: 'personal' | 'organization'): Promise<void> {
  const serviceId = requirement.value?.service_id
  if (!serviceId || busy.value || !props.plugin.enabled || !props.plugin.published) return
  busy.value = scope
  await pluginStore.connectMcpOAuth(props.plugin.id, props.server.id, serviceId, scope === 'organization')
  busy.value = null
}

async function disconnect(scope: 'personal' | 'organization'): Promise<void> {
  const serviceId = requirement.value?.service_id
  if (!serviceId || busy.value) return
  busy.value = scope
  await pluginStore.disconnectMcpOAuth(props.plugin.id, props.server.id, serviceId, scope === 'organization')
  busy.value = null
}
</script>

<template>
  <div class="mt-2 space-y-2 rounded-md border border-border bg-background/60 p-3" :data-testid="`oauth-server-${server.id}`">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="min-w-0">
        <p class="text-sm font-medium">{{ server.name }} · OAuth</p>
        <p class="text-xs text-muted-foreground">Connect an account before attaching its credential to a workspace.</p>
      </div>
      <span v-if="isLoading" class="text-xs text-muted-foreground">Loading status…</span>
    </div>

    <div v-if="requirement" class="grid gap-2 sm:grid-cols-2">
      <div class="flex flex-wrap items-center justify-between gap-2 rounded border border-border px-2.5 py-2">
        <div>
          <p class="text-xs font-medium">Personal account</p>
          <p class="text-xs text-muted-foreground">
            {{ connected('personal') ? 'Connected' : status?.personal.reconnect_required ? 'Reconnect required' : 'Not connected' }}
          </p>
        </div>
        <div class="flex gap-1">
          <Button size="sm" variant="outline" :disabled="!!busy || isLoading || !plugin.enabled || !plugin.published" :data-testid="`oauth-personal-connect-${server.id}`" @click="connect('personal')">
            {{ busy === 'personal' ? 'Starting…' : connected('personal') || status?.personal.reconnect_required ? 'Reconnect' : 'Connect' }}
          </Button>
          <Button v-if="connected('personal') || status?.personal.reconnect_required" size="sm" variant="ghost" :disabled="!!busy" :data-testid="`oauth-personal-disconnect-${server.id}`" @click="disconnect('personal')">Disconnect</Button>
        </div>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-2 rounded border border-border px-2.5 py-2">
        <div>
          <p class="text-xs font-medium">Organization account</p>
          <p class="text-xs text-muted-foreground">
            {{ connected('organization') ? 'Connected' : status?.organization.reconnect_required ? 'Reconnect required' : 'Not connected' }}
          </p>
        </div>
        <div v-if="authStore.isAdmin" class="flex gap-1">
          <Button size="sm" variant="outline" :disabled="!!busy || isLoading || !plugin.enabled || !plugin.published" :data-testid="`oauth-organization-connect-${server.id}`" @click="connect('organization')">
            {{ busy === 'organization' ? 'Starting…' : connected('organization') || status?.organization.reconnect_required ? 'Reconnect' : 'Connect' }}
          </Button>
          <Button v-if="connected('organization') || status?.organization.reconnect_required" size="sm" variant="ghost" :disabled="!!busy" :data-testid="`oauth-organization-disconnect-${server.id}`" @click="disconnect('organization')">Disconnect</Button>
        </div>
        <span v-else class="text-xs text-muted-foreground">
          {{ connected('organization') ? 'Available to attach' : 'Admin only' }}
        </span>
      </div>
    </div>
    <p v-else class="text-xs text-destructive">This server does not have a matching OAuth credential requirement.</p>
  </div>
</template>
