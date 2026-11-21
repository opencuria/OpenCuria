<script setup lang="ts">
/**
 * Add a Windows product key to the pool, or change the label and seats of one.
 * The key is write-only: it is entered once and never shown again.
 */
import { computed, ref, watch } from 'vue'
import { Button } from '@/components/ui/button'
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
import { Label } from '@/components/ui/label'
import type {
  WindowsProductKey,
  WindowsProductKeyCreateInput,
  WindowsProductKeyUpdateInput,
} from '@opencuria/client'

const props = withDefaults(
  defineProps<{
    open: boolean
    /** Key to edit; a new key is added when null. */
    productKey?: WindowsProductKey | null
    saving?: boolean
    error?: string | null
  }>(),
  { productKey: null, saving: false, error: null },
)

const emit = defineEmits<{
  'update:open': [value: boolean]
  create: [input: WindowsProductKeyCreateInput]
  update: [input: WindowsProductKeyUpdateInput]
}>()

/** Five groups of five letters or digits, dashes optional. */
const PRODUCT_KEY_PATTERN = /^([A-Z0-9]{5}-?){4}[A-Z0-9]{5}$/i

const key = ref('')
const label = ref('')
const seats = ref<string | number>(1)

const editing = computed(() => props.productKey !== null)
const minSeats = computed(() => Math.max(1, props.productKey?.used_seats ?? 0))
const keyError = computed(() =>
  !editing.value && key.value.trim() && !PRODUCT_KEY_PATTERN.test(key.value.trim())
    ? 'Enter the 25-character key, e.g. XXXXX-XXXXX-XXXXX-XXXXX-XXXXX.'
    : '',
)
const seatsError = computed(() => {
  const value = Number(seats.value)
  if (!String(seats.value).trim() || !Number.isSafeInteger(value) || value < 1)
    return 'Enter a whole number of seats.'
  if (value < minSeats.value) return `${minSeats.value} seats are in use.`
  return ''
})
const valid = computed(
  () => !seatsError.value && (editing.value || (!!key.value.trim() && !keyError.value)),
)

watch(
  () => props.open,
  (open) => {
    if (!open) return
    key.value = ''
    label.value = props.productKey?.label ?? ''
    seats.value = props.productKey?.seats ?? 1
  },
  { immediate: true },
)

function submit(): void {
  if (!valid.value || props.saving) return
  if (!props.productKey) {
    emit('create', {
      product_key: key.value.trim().toUpperCase(),
      label: label.value.trim(),
      seats: Number(seats.value),
    })
    return
  }
  const input: WindowsProductKeyUpdateInput = {}
  if (label.value.trim() !== props.productKey.label) input.label = label.value.trim()
  if (Number(seats.value) !== props.productKey.seats) input.seats = Number(seats.value)
  if (Object.keys(input).length) emit('update', input)
  else setOpen(false)
}

function setOpen(value: boolean): void {
  if (!props.saving) emit('update:open', value)
}
</script>

<template>
  <Dialog :open="open" @update:open="setOpen">
    <DialogContent class="sm:max-w-md" data-testid="windows-key-dialog">
      <DialogHeader>
        <DialogTitle>{{ editing ? 'Edit product key' : 'Add product key' }}</DialogTitle>
        <DialogDescription>
          {{
            editing
              ? `Key ending in ${productKey?.key_suffix}.`
              : 'Each seat licenses one Windows workspace for its lifetime.'
          }}
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
        <p v-if="error" class="mb-4 text-sm text-destructive" role="alert">{{ error }}</p>
        <form id="windows-key-form" @submit.prevent="submit">
          <fieldset :disabled="saving" class="min-w-0 space-y-4">
            <div v-if="!editing" class="space-y-1.5">
              <Label for="windows-key-value">Product key</Label>
              <Input
                id="windows-key-value"
                v-model="key"
                type="password"
                class="font-mono"
                autocomplete="off"
                spellcheck="false"
                placeholder="XXXXX-XXXXX-XXXXX-XXXXX-XXXXX"
                :aria-invalid="!!keyError || undefined"
              />
              <p v-if="keyError" class="text-xs text-destructive">{{ keyError }}</p>
              <p v-else class="text-xs text-muted-foreground">Encrypted and never shown again.</p>
            </div>
            <div class="space-y-1.5">
              <Label for="windows-key-label">Label</Label>
              <Input
                id="windows-key-label"
                v-model="label"
                maxlength="100"
                placeholder="Optional"
              />
            </div>
            <div class="space-y-1.5">
              <Label for="windows-key-seats">Seats</Label>
              <Input
                id="windows-key-seats"
                :model-value="String(seats)"
                class="w-28"
                type="number"
                :min="minSeats"
                step="1"
                :aria-invalid="!!seatsError || undefined"
                @update:model-value="seats = $event"
              />
              <p v-if="seatsError" class="text-xs text-destructive">{{ seatsError }}</p>
            </div>
          </fieldset>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" :disabled="saving" @click="setOpen(false)">
          Cancel
        </Button>
        <Button
          type="submit"
          form="windows-key-form"
          :disabled="saving || !valid"
          data-testid="windows-key-submit"
        >
          {{ saving ? 'Saving…' : editing ? 'Save' : 'Add key' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
