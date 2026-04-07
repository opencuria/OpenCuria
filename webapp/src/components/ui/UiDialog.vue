<script setup lang="ts">
/**
 * UiDialog — Apple Liquid Glass modal with spring-physics enter/leave.
 *
 * Spring profile: Bouncy (mass=1, stiffness=120, damping=14)
 * Glass overlay: blur(8px) backdrop with OKLCH tint
 * Glass panel: three-layer Liquid Glass
 */
import { ref, watch, onMounted, onUnmounted, computed } from 'vue'
import {
  DialogRoot,
  DialogTrigger,
  DialogPortal,
  DialogOverlay,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogClose,
} from 'radix-vue'
import { X } from 'lucide-vue-next'
import { cn } from '@/lib/utils'

const props = withDefaults(
  defineProps<{
    open?: boolean
    title?: string
    description?: string
    class?: string
  }>(),
  {
    open: undefined,
    title: '',
    description: '',
    class: '',
  },
)

const emit = defineEmits<{
  'update:open': [value: boolean]
}>()

const internalOpen = ref(props.open ?? false)

watch(
  () => props.open,
  (v) => {
    if (v !== undefined) internalOpen.value = v
  },
)

function onOpenChange(val: boolean): void {
  internalOpen.value = val
  emit('update:open', val)
}

// Responsive breakpoint — sm = 640px
const mq = typeof window !== 'undefined' ? window.matchMedia('(min-width: 640px)') : null
const isDesktop = ref(mq?.matches ?? false)

function handleBreakpoint(e: MediaQueryListEvent): void {
  isDesktop.value = e.matches
}

onMounted(() => {
  isDesktop.value = mq?.matches ?? false
  mq?.addEventListener('change', handleBreakpoint)
})

onUnmounted(() => {
  mq?.removeEventListener('change', handleBreakpoint)
})

const desktopStyle = computed(() =>
  isDesktop.value
    ? {
        bottom: 'auto',
        right: 'auto',
        left: '50%',
        top: '50%',
        transform: 'translate(-50%, -50%)',
        width: '100%',
      }
    : {},
)
</script>

<template>
  <DialogRoot :open="internalOpen" @update:open="onOpenChange">
    <DialogTrigger as-child>
      <slot name="trigger" />
    </DialogTrigger>

    <DialogPortal>
      <!-- Glass backdrop -->
      <DialogOverlay
        class="fixed inset-0 z-50 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0"
        style="
          background: oklch(0.10 0.02 248 / 0.30);
          backdrop-filter: blur(8px) saturate(1.1);
          -webkit-backdrop-filter: blur(8px) saturate(1.1);
        "
      />

      <DialogContent
        :class="
          cn(
            // Base: fixed panel (avoid .glass — it adds position:relative which breaks fixed positioning)
            'fixed z-50 w-full flex flex-col max-h-[90dvh] sm:max-w-lg',
            // Animations
            'data-[state=open]:animate-in data-[state=closed]:animate-out',
            'data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0',
            isDesktop ? [
              // Desktop: centered modal with bouncy spring
              'rounded-[var(--radius-lg)]',
              'data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95',
              'data-[state=closed]:slide-out-to-left-1/2 data-[state=open]:slide-in-from-left-1/2',
              'data-[state=closed]:slide-out-to-top-[48%] data-[state=open]:slide-in-from-top-[48%]',
            ] : [
              // Mobile: bottom sheet slide
              'bottom-0 left-0 right-0',
              'rounded-t-[var(--radius-xl)] rounded-b-none',
              'data-[state=closed]:slide-out-to-bottom data-[state=open]:slide-in-from-bottom',
            ],
            props.class,
          )
        "
        :style="[
          desktopStyle,
          {
            background: 'var(--glass-bg)',
            backdropFilter: 'var(--glass-filter)',
            WebkitBackdropFilter: 'var(--glass-filter)',
            border: '1px solid var(--glass-border)',
            boxShadow: 'var(--glass-shadow)',
          }
        ]"
      >
        <!-- Drag handle (mobile only) -->
        <div v-if="!isDesktop" class="flex justify-center pt-3 pb-1 shrink-0">
          <div
            class="w-10 h-1 rounded-full"
            style="background: var(--glass-border-strong)"
          />
        </div>

        <!-- Scrollable content -->
        <div class="overflow-y-auto flex-1 px-6 pt-4 pb-6" :class="isDesktop ? 'sm:p-6' : ''">
          <!-- Header -->
          <div v-if="title || description" class="flex flex-col gap-1.5 mb-4 pr-6">
            <DialogTitle
              v-if="title"
              class="text-lg font-semibold text-fg"
              style="letter-spacing: var(--tracking-title); line-height: var(--lh-heading)"
            >
              {{ title }}
            </DialogTitle>
            <DialogDescription v-if="description" class="text-sm text-muted-fg">
              {{ description }}
            </DialogDescription>
          </div>

          <slot />
        </div>

        <!-- Glass close button -->
        <DialogClose
          class="absolute right-4 top-4 flex items-center justify-center w-7 h-7 rounded-full text-muted-fg hover:text-fg transition-[transform,background,color] duration-150 cursor-pointer"
          style="
            background: var(--glass-bg-subtle);
            backdrop-filter: var(--glass-filter-subtle);
            -webkit-backdrop-filter: var(--glass-filter-subtle);
            border: 1px solid var(--glass-border);
            [transition-timing-function:var(--spring-snappy)];
          "
        >
          <X :size="14" />
        </DialogClose>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
