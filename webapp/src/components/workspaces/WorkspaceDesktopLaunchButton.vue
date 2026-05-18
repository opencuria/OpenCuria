<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Monitor } from 'lucide-vue-next'

import { UiButton } from '@/components/ui'
import type { WorkspaceDesktopStartCommand } from '@/types'

const props = defineProps<{
  commands: WorkspaceDesktopStartCommand[]
  canPrompt: boolean
  desktopOpen: boolean
  desktopMinimized: boolean
}>()

const emit = defineEmits<{
  open: [commandId: string | null]
  restore: []
  minimize: []
}>()

const hostRef = ref<HTMLElement | null>(null)
const dropdownOpen = ref(false)
const dropdownStyle = ref({
  top: '0px',
  left: '0px',
  width: '220px',
})

function updateDropdownPosition(): void {
  const element = hostRef.value
  if (!element) return
  const rect = element.getBoundingClientRect()
  const width = 220
  const padding = 12
  const left = Math.min(
    Math.max(rect.left + rect.width - width, padding),
    window.innerWidth - width - padding,
  )

  dropdownStyle.value = {
    top: `${Math.max(rect.top - 8, padding)}px`,
    left: `${left}px`,
    width: `${width}px`,
  }
}

function closeDropdown(): void {
  dropdownOpen.value = false
}

function handleOpen(commandId: string | null): void {
  closeDropdown()
  emit('open', commandId)
}

function handleClick(): void {
  if (!props.canPrompt) return
  if (!props.desktopOpen) {
    if (props.commands.length > 1) {
      dropdownOpen.value = !dropdownOpen.value
      return
    }
    handleOpen(props.commands[0]?.id ?? null)
    return
  }
  closeDropdown()
  if (props.desktopMinimized) {
    emit('restore')
    return
  }
  emit('minimize')
}

watch(dropdownOpen, async (open) => {
  if (!open) return
  await nextTick()
  updateDropdownPosition()
})

watch(
  () => [props.desktopOpen, props.desktopMinimized, props.commands.length],
  () => {
    if (props.desktopOpen || props.commands.length <= 1) {
      closeDropdown()
    }
  },
)

const handleWindowUpdate = () => {
  if (dropdownOpen.value) updateDropdownPosition()
}

onMounted(() => {
  window.addEventListener('resize', handleWindowUpdate)
  window.addEventListener('scroll', handleWindowUpdate, true)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleWindowUpdate)
  window.removeEventListener('scroll', handleWindowUpdate, true)
})
</script>

<template>
  <div ref="hostRef" class="shrink-0 mr-2 mb-2">
    <UiButton
      variant="ghost"
      size="icon-sm"
      :disabled="!canPrompt"
      :title="desktopOpen ? (desktopMinimized ? 'Restore desktop' : 'Minimize desktop') : (commands.length > 1 ? 'Choose desktop start command' : 'Open desktop')"
      @click="handleClick"
    >
      <span class="relative inline-flex">
        <Monitor :size="16" :class="desktopOpen ? 'text-primary' : ''" />
        <span
          v-if="desktopOpen && desktopMinimized"
          class="absolute -bottom-1 -right-1 h-2 w-2 rounded-full bg-primary"
          title="Desktop minimized"
        />
      </span>
    </UiButton>

    <Teleport to="body">
      <template v-if="dropdownOpen">
        <div class="fixed inset-0 z-[120]" @click="closeDropdown" />
        <div
          class="fixed z-[121] -translate-y-full rounded-[var(--radius-md)] glass-strong py-1"
          :style="dropdownStyle"
        >
          <button
            v-for="command in commands"
            :key="command.id"
            type="button"
            class="flex w-full flex-col items-start gap-0.5 px-3 py-2 text-left text-sm text-fg transition-colors hover:bg-surface-hover"
            @click="handleOpen(command.id)"
          >
            <span class="font-medium">{{ command.name }}</span>
            <span class="w-full truncate text-xs text-muted-fg">{{ command.command }}</span>
          </button>
        </div>
      </template>
    </Teleport>
  </div>
</template>
