<!--
  WindowsLicensingTab — how Windows workspaces are licensed: the license model, the
  product key pool, licensed members and an optional cap on running Windows workspaces.
  Admin-only: SettingsSheet never shows it to members, and the backend enforces it.
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChevronDown, Key, Pencil, Plus, Trash2, X } from '@lucide/vue'
import {
  addWindowsProductKey,
  deleteWindowsProductKey,
  getWindowsLicensing,
  setWindowsUserLicensed,
  updateWindowsLicensing,
  updateWindowsProductKey,
} from '@/services/licensing.api'
import { useAuthStore } from '@/stores/auth'
import { useAutosave } from '@/composables/useAutosave'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import EmptyState from '@/components/common/EmptyState.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import AutosaveStatus from './AutosaveStatus.vue'
import SettingsRow from './SettingsRow.vue'
import SettingsSection from './SettingsSection.vue'
import WindowsProductKeyDialog from './WindowsProductKeyDialog.vue'
import { windowsActivationLabel } from '@opencuria/chat-core/windowsLicense'
import type {
  WindowsLicenseMode,
  WindowsLicenseUsage,
  WindowsLicensing,
  WindowsLicensingInput,
  WindowsProductKey,
  WindowsProductKeyCreateInput,
  WindowsProductKeyUpdateInput,
} from '@opencuria/client'

const MODES: { value: WindowsLicenseMode; label: string; description: string }[] = [
  {
    value: 'per_device',
    label: 'Product keys',
    description: 'Each Windows workspace holds one key seat for its lifetime.',
  },
  {
    value: 'per_user',
    label: 'Per user',
    description: 'Windows workspaces of licensed members activate through KMS.',
  },
  {
    value: 'unlicensed',
    label: 'Without license',
    description: "Windows runs without activation. Microsoft's terms still require a license.",
  },
]

/** Host name or IP address, optionally with a port. */
const KMS_HOST_PATTERN = /^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:\d{1,5})?$/

const authStore = useAuthStore()
const activeOrganizationId = computed(() => authStore.activeOrganizationId)

const loading = ref(true)
const loadError = ref<string | null>(null)
const licensing = ref<WindowsLicensing | null>(null)
const actionError = ref<string | null>(null)

const mode = ref<WindowsLicenseMode>('per_device')
const kmsHost = ref('')
const userSeats = ref<string | number>(0)
const capEnabled = ref(false)
const maxRunning = ref<string | number>(1)

const memberBusy = ref<string | null>(null)
const keyDialogOpen = ref(false)
const editingKey = ref<WindowsProductKey | null>(null)
const keySaving = ref(false)
const keyError = ref<string | null>(null)
const deletingKey = ref<WindowsProductKey | null>(null)
const deleteBusy = ref(false)
const deleteError = ref<string | null>(null)

const modeDescription = computed(() => MODES.find((item) => item.value === mode.value)?.description)
const keys = computed(() => licensing.value?.product_keys ?? [])
const showKeys = computed(() => mode.value === 'per_device' || keys.value.length > 0)
const licensedCount = computed(
  () => licensing.value?.members.filter((member) => member.licensed).length ?? 0,
)
const capSummary = computed(() =>
  capEnabled.value && Number(maxRunning.value) >= 1
    ? `At most ${Number(maxRunning.value)} run at the same time.`
    : 'No limit.',
)
const usageSummary = computed(() => (licensing.value ? describeUsage(licensing.value.usage) : ''))

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

function describeUsage(usage: WindowsLicenseUsage): string {
  return [
    plural(usage.windows_workspaces, 'Windows workspace'),
    `${usage.running} running`,
    usage.device_key ? `${usage.device_key} on product keys` : '',
    usage.user ? `${usage.user} per user` : '',
    usage.unlicensed ? `${usage.unlicensed} without license` : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

function isWholeNumber(value: string | number, min: number): boolean {
  const number = Number(value)
  return String(value).trim() !== '' && Number.isSafeInteger(number) && number >= min
}

const seatsValid = computed(() => isWholeNumber(userSeats.value, 0))
const kmsValid = computed(
  () => !kmsHost.value.trim() || KMS_HOST_PATTERN.test(kmsHost.value.trim()),
)
const validationError = computed(() => {
  if (mode.value === 'per_user' && !seatsValid.value)
    return 'Enter a whole number of user licenses.'
  if (mode.value === 'per_user' && !kmsValid.value)
    return 'Enter a host name or IP address, optionally with :port.'
  if (capEnabled.value && !isWholeNumber(maxRunning.value, 1))
    return 'Enter a running limit of at least 1.'
  return ''
})

/** Only the fields that differ from the server; hidden per-user fields keep their value. */
const changes = computed<WindowsLicensingInput>(() => {
  const base = licensing.value
  if (!base) return {}
  const input: WindowsLicensingInput = {}
  if (mode.value !== base.mode) input.mode = mode.value
  if (mode.value === 'per_user') {
    if (kmsHost.value.trim() !== base.kms_host) input.kms_host = kmsHost.value.trim()
    if (Number(userSeats.value) !== base.user_license_seats)
      input.user_license_seats = Number(userSeats.value)
  }
  const cap = capEnabled.value ? Number(maxRunning.value) : null
  if (cap !== base.max_running) input.max_running = cap
  return input
})

function formSnapshot(): string {
  return JSON.stringify([
    mode.value,
    kmsHost.value,
    userSeats.value,
    capEnabled.value,
    maxRunning.value,
  ])
}

function applyForm(state: WindowsLicensing): void {
  mode.value = state.mode
  kmsHost.value = state.kms_host
  userSeats.value = state.user_license_seats
  capEnabled.value = state.max_running !== null
  maxRunning.value = state.max_running ?? 1
}

const autosave = useAutosave({
  source: formSnapshot,
  dirty: () => Object.keys(changes.value).length > 0,
  enabled: () => !loading.value && !!licensing.value && !loadError.value,
  valid: () => !validationError.value,
  errorMessage: 'Failed to update Windows licensing',
  save: async (isCurrent) => {
    const input = { ...changes.value }
    if (!Object.keys(input).length) return
    const sent = formSnapshot()
    const updated = await updateWindowsLicensing(activeOrganizationId.value!, input)
    if (!isCurrent()) return
    licensing.value = updated
    // Take server normalization (e.g. a trimmed host) unless the form changed meanwhile.
    if (formSnapshot() === sent) applyForm(updated)
  },
})

// Leaving per-user mode drops per-user input the server would reject anyway.
watch(mode, (value) => {
  if (value === 'per_user' || !licensing.value) return
  if (!seatsValid.value) userSeats.value = licensing.value.user_license_seats
  if (!kmsValid.value) kmsHost.value = licensing.value.kms_host
})

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

async function loadData(): Promise<void> {
  const organizationId = activeOrganizationId.value
  loading.value = true
  loadError.value = null
  try {
    if (!organizationId) throw new Error('No active organization selected')
    const next = await getWindowsLicensing(organizationId)
    if (organizationId !== activeOrganizationId.value) return
    licensing.value = next
    applyForm(next)
  } catch (e: unknown) {
    if (organizationId !== activeOrganizationId.value) return
    loadError.value = errorText(e, 'Failed to load Windows licensing')
  } finally {
    if (organizationId === activeOrganizationId.value) loading.value = false
  }
}

onMounted(() => void loadData())
watch(activeOrganizationId, () => void loadData())

function openKeyDialog(key: WindowsProductKey | null): void {
  editingKey.value = key
  keyError.value = null
  keyDialogOpen.value = true
}

function replaceKey(key: WindowsProductKey): void {
  if (!licensing.value) return
  const others = licensing.value.product_keys.filter((item) => item.id !== key.id)
  const index = licensing.value.product_keys.findIndex((item) => item.id === key.id)
  others.splice(index === -1 ? others.length : index, 0, key)
  licensing.value = { ...licensing.value, product_keys: others }
}

async function saveKey(
  input: WindowsProductKeyCreateInput | WindowsProductKeyUpdateInput,
): Promise<void> {
  const organizationId = activeOrganizationId.value
  if (!organizationId || keySaving.value) return
  keySaving.value = true
  keyError.value = null
  try {
    const saved = editingKey.value
      ? await updateWindowsProductKey(organizationId, editingKey.value.id, input)
      : await addWindowsProductKey(organizationId, input as WindowsProductKeyCreateInput)
    replaceKey(saved)
    keyDialogOpen.value = false
  } catch (e: unknown) {
    keyError.value = errorText(e, 'Failed to save the product key')
  } finally {
    keySaving.value = false
  }
}

function askDelete(key: WindowsProductKey): void {
  deleteError.value = null
  deletingKey.value = key
}

function setDeleteOpen(open: boolean): void {
  if (!open && !deleteBusy.value) deletingKey.value = null
}

async function confirmDelete(): Promise<void> {
  const organizationId = activeOrganizationId.value
  const key = deletingKey.value
  if (!organizationId || !key || deleteBusy.value) return
  deleteBusy.value = true
  deleteError.value = null
  try {
    await deleteWindowsProductKey(organizationId, key.id)
    if (licensing.value)
      licensing.value = {
        ...licensing.value,
        product_keys: licensing.value.product_keys.filter((item) => item.id !== key.id),
      }
    deletingKey.value = null
  } catch (e: unknown) {
    deleteError.value = errorText(e, 'Failed to delete the product key')
  } finally {
    deleteBusy.value = false
  }
}

async function setLicensed(userId: string, licensed: boolean): Promise<void> {
  const organizationId = activeOrganizationId.value
  if (!organizationId || memberBusy.value) return
  memberBusy.value = userId
  actionError.value = null
  try {
    const updated = await setWindowsUserLicensed(organizationId, userId, licensed)
    if (organizationId === activeOrganizationId.value) licensing.value = updated
  } catch (e: unknown) {
    actionError.value = errorText(e, 'Failed to change the user license')
  } finally {
    memberBusy.value = null
  }
}

function keyTitle(key: WindowsProductKey): string {
  return key.label || 'Product key'
}
</script>

<template>
  <div class="space-y-6">
    <div v-if="loading" class="flex justify-center py-12">
      <LoadingSpinner :size="24" />
    </div>

    <div
      v-else-if="loadError"
      class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
      role="alert"
    >
      {{ loadError }}
    </div>

    <template v-else-if="licensing">
      <div
        v-if="actionError"
        class="flex items-center justify-between gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
        role="alert"
        data-testid="windows-action-error"
      >
        <span>{{ actionError }}</span>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Dismiss error"
          @click="actionError = null"
        >
          <X />
        </Button>
      </div>

      <SettingsSection
        title="Windows licensing"
        description="Applies when Windows workspaces are created or started."
      >
        <div class="overflow-hidden rounded-lg border border-border bg-card">
          <div class="flex items-center justify-between gap-4 px-4 py-4">
            <div class="min-w-0 flex-1 space-y-1">
              <label for="windows-license-mode" class="text-sm font-medium text-foreground">
                License model
              </label>
              <p class="text-sm text-muted-foreground" data-testid="windows-mode-description">
                {{ modeDescription }}
              </p>
            </div>
            <Select v-model="mode">
              <SelectTrigger id="windows-license-mode" class="w-44" data-testid="windows-mode">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="item in MODES" :key="item.value" :value="item.value">
                  {{ item.label }}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          <template v-if="mode === 'per_user'">
            <div class="flex items-center justify-between gap-4 border-t border-border px-4 py-4">
              <div class="min-w-0 flex-1 space-y-1">
                <label for="windows-user-seats" class="text-sm font-medium text-foreground">
                  User licenses
                </label>
                <p class="text-sm text-muted-foreground">
                  {{ licensedCount }} assigned to members below.
                </p>
              </div>
              <Input
                id="windows-user-seats"
                class="w-24"
                :model-value="String(userSeats)"
                type="number"
                min="0"
                step="1"
                @update:model-value="userSeats = $event"
              />
            </div>
            <div class="flex items-center justify-between gap-4 border-t border-border px-4 py-4">
              <label for="windows-kms-host" class="min-w-0 flex-1 text-sm font-medium">
                KMS host
              </label>
              <Input
                id="windows-kms-host"
                v-model="kmsHost"
                class="w-56 font-mono"
                placeholder="Automatic (DNS)"
                spellcheck="false"
              />
            </div>
          </template>

          <div class="flex items-center justify-between gap-4 border-t border-border px-4 py-4">
            <label for="windows-running-cap" class="min-w-0 flex-1 cursor-pointer space-y-1">
              <div class="text-sm font-medium text-foreground">Limit running workspaces</div>
              <p class="text-sm text-muted-foreground">{{ capSummary }}</p>
            </label>
            <Input
              v-if="capEnabled"
              id="windows-max-running"
              class="w-20"
              aria-label="Maximum running Windows workspaces"
              :model-value="String(maxRunning)"
              type="number"
              min="1"
              step="1"
              @update:model-value="maxRunning = $event"
            />
            <Switch id="windows-running-cap" v-model="capEnabled" />
          </div>
        </div>
        <p class="text-xs text-muted-foreground" data-testid="windows-usage">{{ usageSummary }}</p>
        <p v-if="validationError" class="text-xs text-destructive" role="alert">
          {{ validationError }}
        </p>
        <AutosaveStatus :state="autosave" data-testid="windows-licensing-status" />
      </SettingsSection>

      <SettingsSection
        v-if="mode === 'per_user'"
        title="Licensed members"
        description="Their Windows workspaces activate through KMS."
        data-testid="windows-members"
      >
        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <div
            v-for="member in licensing.members"
            :key="member.user_id"
            class="flex items-center justify-between gap-4 px-4 py-3"
            :data-testid="`windows-member-${member.user_id}`"
          >
            <span class="min-w-0 truncate text-sm">{{ member.label }}</span>
            <Switch
              :model-value="member.licensed"
              :disabled="memberBusy !== null"
              :aria-label="`Windows license for ${member.label}`"
              @update:model-value="setLicensed(member.user_id, $event)"
            />
          </div>
        </div>
      </SettingsSection>

      <SettingsSection
        v-if="showKeys"
        title="Product keys"
        :description="
          mode === 'per_device'
            ? 'New Windows workspaces take a seat from the oldest key with free seats.'
            : 'Assigned seats stay valid in every license model.'
        "
        data-testid="windows-keys"
      >
        <template v-if="mode === 'per_device'" #actions>
          <Button size="sm" data-testid="windows-key-add" @click="openKeyDialog(null)">
            <Plus /> Add key
          </Button>
        </template>

        <div v-if="!keys.length" class="overflow-hidden rounded-lg border border-border bg-card">
          <EmptyState
            :icon="Key"
            title="No product keys"
            description="Windows workspaces need a key with a free seat."
          />
        </div>
        <TooltipProvider v-else>
          <div
            class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
          >
            <SettingsRow v-for="key in keys" :key="key.id" :data-testid="`windows-key-${key.id}`">
              <template #icon><Key :size="16" /></template>
              <div class="min-w-0 space-y-0.5">
                <div class="truncate text-sm font-medium">{{ keyTitle(key) }}</div>
                <div class="font-mono text-xs text-muted-foreground">…{{ key.key_suffix }}</div>
              </div>
              <template #badges>
                <Badge variant="outline" :title="`${key.used_seats} of ${key.seats} seats used`">
                  {{ key.used_seats }}/{{ key.seats }}
                </Badge>
              </template>
              <template #actions>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  :aria-label="`Edit ${keyTitle(key)}`"
                  :data-testid="`windows-key-edit-${key.id}`"
                  @click="openKeyDialog(key)"
                >
                  <Pencil />
                </Button>
                <Tooltip :disabled="key.used_seats === 0">
                  <TooltipTrigger as-child>
                    <span class="inline-flex">
                      <Button
                        size="icon-sm"
                        variant="ghost"
                        :aria-label="`Delete ${keyTitle(key)}`"
                        :disabled="key.used_seats > 0"
                        :data-testid="`windows-key-delete-${key.id}`"
                        @click="askDelete(key)"
                      >
                        <Trash2 />
                      </Button>
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>
                    In use by {{ key.used_seats }} workspace{{ key.used_seats === 1 ? '' : 's' }}
                  </TooltipContent>
                </Tooltip>
              </template>
              <template v-if="key.assignments.length" #detail>
                <Collapsible>
                  <CollapsibleTrigger
                    class="group inline-flex items-center gap-1 rounded-sm text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
                    :data-testid="`windows-key-assignments-${key.id}`"
                  >
                    {{ key.assignments.length }} assigned
                    <ChevronDown
                      class="size-3.5 transition-transform group-data-[state=open]:rotate-180"
                      aria-hidden="true"
                    />
                  </CollapsibleTrigger>
                  <CollapsibleContent>
                    <ul class="mt-2 space-y-1 text-xs">
                      <li
                        v-for="(assignment, index) in key.assignments"
                        :key="index"
                        class="flex items-center justify-between gap-3"
                      >
                        <span class="min-w-0 truncate">{{ assignment.owner_label }}</span>
                        <span class="shrink-0 text-muted-foreground">
                          {{ windowsActivationLabel(assignment.activation_status).label }}
                        </span>
                      </li>
                    </ul>
                  </CollapsibleContent>
                </Collapsible>
              </template>
            </SettingsRow>
          </div>
        </TooltipProvider>
      </SettingsSection>
    </template>

    <WindowsProductKeyDialog
      v-model:open="keyDialogOpen"
      :product-key="editingKey"
      :saving="keySaving"
      :error="keyError"
      @create="saveKey"
      @update="saveKey"
    />

    <Dialog :open="deletingKey !== null" @update:open="setDeleteOpen">
      <DialogContent class="sm:max-w-md" data-testid="windows-key-delete-dialog">
        <DialogHeader>
          <DialogTitle>Delete product key?</DialogTitle>
          <DialogDescription>
            The key ending in {{ deletingKey?.key_suffix }} is removed from the pool.
          </DialogDescription>
        </DialogHeader>
        <DialogBody v-if="deleteError">
          <p class="text-sm text-destructive" role="alert">{{ deleteError }}</p>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" :disabled="deleteBusy" @click="setDeleteOpen(false)">
            Cancel
          </Button>
          <Button
            variant="destructive"
            :disabled="deleteBusy"
            data-testid="windows-key-delete-confirm"
            @click="confirmDelete"
          >
            {{ deleteBusy ? 'Deleting…' : 'Delete' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
